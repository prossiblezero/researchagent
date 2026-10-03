"""Authorized bounded research -> Codex -> measured experiments on the existing worker."""
from __future__ import annotations

import difflib
import json
import os
import stat
import subprocess
import threading
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from .experiment_process import ROOT, clean_environment, coding_command, codex_path, codex_events, run_process
from .experiment_retrieval import BASELINE, SUITE, configuration, dump, evaluate, frozen_data, metric_contract, sha
from . import experiment_iteration as iteration
from . import experiment_feedback as feedback
from . import experiment_evidence as evidence
from . import experiment_retrieval as legacy
from .research_records import encoded, digest, fields
from .trace import now_iso, redact
from .usage import normalize_usage, summarize_usage
from .workbench_store import Conflict, NotFound, text_field


def initialize(db):
    db.execute('''CREATE TABLE IF NOT EXISTS experiment_runs (
        job_id TEXT PRIMARY KEY REFERENCES research_jobs(id), space_id TEXT NOT NULL REFERENCES research_spaces(id),
        input_version_id TEXT NOT NULL REFERENCES experiment_versions(id), contract TEXT NOT NULL,
        state TEXT NOT NULL, result_version_id TEXT REFERENCES experiment_versions(id), updated_at TEXT NOT NULL)''')


def registered(name):
    if name == SUITE: return legacy
    if name == feedback.SUITE: return feedback
    if name == evidence.SUITE: return evidence
    raise ValueError('未知注册实验')


class Experiments:
    def __init__(self, app):
        self.app, self.store, self.records = app, app.store, app.records
        self.root = self.store.path.resolve().parent / 'experiment-runs'
        # Keep generated code outside private data, which the native sandbox denies.
        self.work_root = ROOT / 'experiments'
        self.lock = threading.Lock()

    def prepare(self, space_id, body):
        fields(body, ('title', 'hypothesis', 'idea_id', 'suite'))
        suite = registered(body.get('suite', feedback.SUITE))
        hypothesis = text_field(body.get('hypothesis'), '待验证假设', 1960)
        if body.get('idea_id') and body['idea_id'] not in {i['id'] for i in self.records.ideas(space_id)}:
            raise NotFound('创新候选不属于当前研究区')
        title = text_field(body.get('title', '公开语料检索实验'), '实验名称', 200)
        config = suite.configuration()
        baseline = self.records.save_experiment(space_id, {'kind': 'baseline', 'title': title + (' · 混合排序基线' if suite is not legacy else ' · FTS5 基线'),
            'status': 'planned', 'config': config, 'metrics': suite.metric_contract(), 'note': 'V4 注册评测；复用已有检索机制，运行后产生实测版本'})
        body = {'kind': 'experiment', 'title': title, 'idea_id': body.get('idea_id'), 'status': 'planned',
                'baseline_version_id': baseline['versions'][-1]['id'], 'config': config,
                'metrics': suite.metric_contract(), 'note': 'HYPOTHESIS（未验证）：' + hypothesis}
        return self.records.save_experiment(space_id, body)

    def submit(self, space_id, version_id, body):
        fields(body, ('conversation_id', 'authorize_execution', 'seconds', 'token_budget', 'max_iterations'))
        if body.get('authorize_execution') is not True:
            raise ValueError('执行请求须明确包含 authorize_execution=true')
        seconds, tokens = body.get('seconds', 240), body.get('token_budget', 300000)
        if type(seconds) is not int or not 30 <= seconds <= 600 or type(tokens) is not int or not 2000 <= tokens <= 500000:
            raise ValueError('编码预算为 30–600 秒、2000–500000 token（包含缓存输入）')
        rounds = body.get('max_iterations', 1)
        if type(rounds) is not int or not 1 <= rounds <= 3:
            raise ValueError('max_iterations 必须是 1–3 的整数')
        codex_path()
        with closing(self.store._connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            version = self.records.experiment_version(db, space_id, version_id)
            baseline = self.records.experiment_version(db, space_id, version['baseline_version_id'])
            suite = registered(version['content']['config']['dataset'])
            if rounds > 1 and suite is legacy:
                raise ValueError('有限多轮仅支持开发反馈或原文证据套件')
            expected = suite.configuration()
            for v in (version, baseline):
                if v['content']['config'] != expected or v['content']['status'] != 'planned':
                    raise ValueError('首版仅执行「创建检索实验」生成的未运行冻结方案；任意命令仍仅记录')
                latest=db.execute('SELECT id FROM experiment_versions WHERE experiment_id=? ORDER BY version DESC LIMIT 1',(v['experiment_id'],)).fetchone()[0]
                if latest != v['id']:raise Conflict('方案或基线已有新版本，请刷新后重新锁定')
                reference={m['name']:m for m in suite.metric_contract()}
                actual={m['name']:m for m in v['content']['metrics']}
                if actual.keys()!=reference.keys() or any(any(actual[n][k]!=reference[n][k] for k in ('unit','direction')) for n in reference):
                    raise ValueError('指标名称、单位和方向须匹配注册评测器')
            if version['record']['kind'] != 'experiment' or baseline['record']['kind'] != 'baseline':
                raise ValueError('需要实验及固定基线')
            if {m['name'] for m in version['content']['metrics']} != {m['name'] for m in suite.metric_contract()}:
                raise ValueError('指标须匹配注册评测器')
            existing = db.execute('SELECT j.status FROM experiment_runs r JOIN research_jobs j ON j.id=r.job_id WHERE r.input_version_id=?', (version_id,)).fetchall()
            if any(r[0] in ('queued', 'running', 'completed') for r in existing):
                raise Conflict('这个方案已提交或完成；失败可重试，完成后请创建新方案')
            conversation_id = body.get('conversation_id')
            self.store._require(db, 'conversations', conversation_id, space_id)
            message = self.store._message(db, conversation_id, 'user', '运行受控实验：' + version['record']['title'], 'EXPERIMENT')
            job = self.store._enqueue(db, space_id, conversation_id, version['record']['title'], version['content']['note'], [],
                                      kind='EXPERIMENT', payload={'version_id': version_id, 'parent_message_id': message['id']})
            contract = {'schema_version': 'research-execution/v2' if rounds > 1 else 'research-execution/v1', 'suite': suite.SUITE, 'input': version, 'baseline': baseline,
                        'model': 'gpt-5.6-luna', 'budget': {'coding_seconds': seconds, 'token_budget': tokens,
                            'coding_invocations': rounds, 'tool_calls': 20, 'evaluation_attempts': 3, 'evaluation_seconds': 30,
                            'development_candidates': 2 * rounds if suite is not legacy else 0},
                        'workspace': str(self.work_root / job['id']), 'authorization': {'explicit': True, 'at': now_iso()},
                        'boundary': 'Windows elevated：仅实验目录写入、命令禁网、私有数据/凭据拒读；其他文件仍有系统级读取。非虚拟机。'}
            if rounds > 1: contract['iteration_policy_hash'] = iteration.policy_hash(self)
            db.execute('INSERT INTO experiment_runs VALUES(?,?,?,?,?,NULL,?)',
                       (job['id'], space_id, version_id, encoded(contract), encoded({'phase': 'queued', 'coding_invocations': 0, 'evaluation_attempts': 0}), now_iso()))
        self.app.wake.set()
        return self.get(space_id, job['id'])

    def get(self, space_id, job_id):
        job = self.store.job(space_id, job_id)
        with closing(self.store._connect()) as db:
            row = db.execute('SELECT * FROM experiment_runs WHERE job_id=? AND space_id=?', (job_id, space_id)).fetchone()
        if not row: raise NotFound('执行记录不存在')
        result = dict(row)
        result.update(job=job, contract=json.loads(row['contract']), state=json.loads(row['state']))
        return result

    def list(self, space_id):
        self.store.space(space_id)
        with closing(self.store._connect()) as db:
            ids = [r[0] for r in db.execute('SELECT job_id FROM experiment_runs WHERE space_id=? ORDER BY updated_at DESC', (space_id,))]
        result = []
        for job_id in ids:
            try: result.append(self.get(space_id, job_id))
            except NotFound: pass
        return result

    def save(self, job, state):
        with closing(self.store._connect()) as db, db:
            changed = db.execute('''UPDATE experiment_runs SET state=?,updated_at=? WHERE job_id=? AND EXISTS
                (SELECT 1 FROM research_jobs WHERE id=? AND status='running' AND execution_generation=?)''',
                (encoded(state), now_iso(), job['id'], job['id'], job['execution_generation'])).rowcount
        if not changed: raise Conflict('执行已停止或执行代次已改变')
        self.app.sessions.save_state(job, {'v4': True, 'phase': state['phase']})

    def resume(self, space_id, job_id):
        with self.lock:
            run = self.get(space_id, job_id)
            if (run['contract']['budget'].get('coding_invocations', 1) == 1 and
                    run['state'].get('coding_invocations') and not run['state'].get('code_hash') and
                    not self.coding_receipt(self.root/job_id)):
                raise Conflict('编码未形成有效检查点；请重试新尝试，不重复花费旧预算')
            if run['state'].get('evaluation_attempts', 0) >= 3 and not run['state'].get('candidate'):
                raise Conflict('本次测量预算已用尽，请创建新尝试')
            if run['state'].get('final_evaluation_status') == 'started' and not run['state'].get('candidate'):
                raise Conflict('最终测试已占用且无完整检查点，本次终验不自动重放；已保留失败与冻结代码')
            return self.app.sessions.resume(space_id, job_id)

    @staticmethod
    def coding_receipt(output):
        """Reuse a completed host-written receipt after postprocessing/restart failure, never a partial turn."""
        try:
            coding=json.loads((output/'codex.json').read_text(encoding='utf-8'))
            events=[json.loads(line) for line in (output/'events.jsonl').read_text(encoding='utf-8').splitlines()]
            terminals=[e for e in events if e.get('type') in ('turn.completed','turn.failed')]
            # CLI emits recoverable reconnection errors before a successful terminal event.
            if coding['termination']=='completed' and coding['exit_code']==0 and coding.get('candidate_hash') and terminals and terminals[-1]['type']=='turn.completed' and not any(e.get('type')=='invalid_jsonl' for e in events):
                return events
        except (OSError,ValueError,KeyError):pass
        return None

    @staticmethod
    def preserve_evidence_usage(output, data):
        """Keep per-job paid receipts when recovery replays shared feature caches."""
        path = output / 'semantic-signals.json'
        previous = json.loads(path.read_text(encoding='utf-8')) if path.is_file() else {}
        old = previous.get('features', {}).get('evidence_rerank', {})
        records = old.get('run_usage_records')
        if records is None:
            receipt = output / 'evidence-rerank.json'
            prior = json.loads(receipt.read_text(encoding='utf-8')) if receipt.is_file() else []
            records = [{**usage, 'cache_key': row['cache_key'], 'question_id': row['question_id']}
                       for row in prior if not row['cache_hit'] for usage in row['usage_records']]
        records = records + [{**usage, 'cache_key': row['cache_key'], 'question_id': row['question_id']}
                             for row in data['evidence_records'] if not row['cache_hit'] for usage in row['usage_records']]
        stats = data['features']['evidence_rerank']
        stats['latest_preparation_usage'] = stats['current_usage']
        stats['preparation_history'] = old.get('preparation_history', []) + [
            {'cache_hits': stats['cache_hits'], 'seconds': stats['preparation_seconds'],
             'usage': stats['latest_preparation_usage']}]
        stats['run_usage_records'] = records
        stats['current_usage'] = summarize_usage(records)
        stats['usage_scope'] = 'current_usage accumulates paid requests for this job across recovery; latest_preparation_usage and cache_hits describe this preparation only.'

    @staticmethod
    def code_snapshot(workspace):
        path=workspace/'ranker.py'
        for item in (workspace,path):
            info=item.lstat()
            if item.is_symlink() or getattr(info,'st_file_attributes',0) & stat.FILE_ATTRIBUTE_REPARSE_POINT or item.is_file() and info.st_nlink!=1:
                raise ValueError('实验路径不能包含链接')
        if not path.is_file() or path.stat().st_size>2*1024*1024:raise ValueError('候选代码不存在或超出大小上限')
        return path.read_text(encoding='utf-8')

    @staticmethod
    def candidate_slot_bytes(workspace, name):
        """Read a bounded regular slot only after checking its filesystem identity."""
        path = workspace / name
        for item in (workspace, path):
            info = item.lstat()
            if item.is_symlink() or getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT or item.is_file() and info.st_nlink != 1:
                raise ValueError('开发候选快照不能包含链接')
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode) or before.st_size > 2 * 1024 * 1024:
            raise ValueError('开发候选快照不存在或超出大小上限')
        with path.open('rb') as file:
            opened = os.fstat(file.fileno())
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino) or opened.st_nlink != 1:
                raise Conflict('读取开发候选快照时路径发生变化')
            content = file.read(2 * 1024 * 1024 + 1)
        after = path.lstat()
        if len(content) > 2 * 1024 * 1024 or any(getattr(before, key) != getattr(after, key) for key in ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns')):
            raise Conflict('读取开发候选快照时文件发生变化')
        return content

    def retry(self, space_id, job_id):
        run = self.get(space_id, job_id)
        if run['job']['status'] not in ('failed', 'interrupted', 'cancelled'):
            raise Conflict('只能重试失败、中断或取消的实验')
        return self.submit(space_id, run['input_version_id'], {'conversation_id': run['job']['conversation_id'],
            'authorize_execution': True, 'seconds': run['contract']['budget']['coding_seconds'],
            'token_budget': run['contract']['budget']['token_budget'],
            'max_iterations': run['contract']['budget'].get('coding_invocations', 1)})['job']

    def artifact(self, space_id, job_id, name):
        self.get(space_id, job_id)
        if name not in {'contract.json', 'sources.json', 'baseline.json', 'candidate.json', 'codex.json',
                        'events.jsonl', 'diff.patch', 'result.json', 'ranker.py', 'failure.json',
                        'dev-feedback.json', 'dev-inputs.json', 'dev-eval.py', 'proposal.md', 'semantic-signals.json',
                        'development-snapshots.json', 'submitted-ranker.py', 'evidence-rerank.json', 'iterations.json'} and not iteration.artifact_name(name):
            raise NotFound('产物不存在')
        path = self.root / job_id / (iteration.artifact_name(name) or name)
        if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(self.root.resolve()):
            raise NotFound('产物不存在')
        return path

    def execute(self, job):
        # Same worker as research; lock also prevents resume while a cancelled process is being reaped.
        with self.lock:
            try:self._execute(job)
            except (Conflict,NotFound):
                # Cancellation/soft deletion can win between any two persistent operations.
                self.store.finish(job['id'],'failed','实验已停止或输入失效；已有产物保留。')
            except Exception as exc:
                self.store.finish(job['id'],'failed','实验基础设施失败；已有产物保留。',str(redact(str(exc)))[:1000])

    def _execute(self, job):
        run = self.get(job['space_id'], job['id']); contract, state = run['contract'], run['state']
        suite = registered(contract['suite'])
        baseline_code = suite.BASELINE
        workspace = Path(contract['workspace']); output = self.root / job['id']
        output.mkdir(parents=True, exist_ok=True)
        private = [self.root, self.store.path, ROOT / 'tests']
        def cancelled():
            try:current = self.store.job(job['space_id'], job['id'])
            except NotFound:return True
            return self.app.stop.is_set() or current['status'] != 'running' or current['execution_generation'] != job['execution_generation']
        def stage(name, message):
            if cancelled(): raise Conflict('执行已停止')
            state['phase'] = name; self.save(job, state)
            self.store.progress(job['id'], name, message, '')
            self.app.sessions.event(job, 'experiment_stage', {'phase': name, 'message': message})
        def tree():
            found = {}; total = 0
            expected=state.get('prepared')
            for count,path in enumerate(workspace.rglob('*'),1):
                if cancelled():raise Conflict('执行已停止')
                if count>500:raise ValueError('候选目录条目超出上限')
                info = path.lstat()
                if path.is_symlink() or getattr(info,'st_file_attributes',0) & stat.FILE_ATTRIBUTE_REPARSE_POINT or path.is_file() and info.st_nlink != 1:
                    raise ValueError('候选目录不允许链接或重解析路径')
                if path.is_file():
                    relative=path.relative_to(workspace).as_posix()
                    if expected is not None and relative not in expected:raise ValueError('候选创建了合同外文件')
                    if info.st_size > 2 * 1024 * 1024: raise ValueError('候选文件超过上限')
                    total+=info.st_size
                    if total>8*1024*1024:raise ValueError('候选目录总大小超过上限')
                    found[relative] = sha(path.read_bytes())
            return found
        def measured(label):
            if state['evaluation_attempts'] >= 3: raise Conflict('测量次数预算已耗尽')
            state['evaluation_attempts'] += 1; self.save(job, state)
            result = (suite.evaluate if suite is not legacy else evaluate)(workspace, data, cancelled, private)
            dump(output / (label + '.json'), result)
            if not result['valid']: raise RuntimeError(label + ': ' + result['error'])
            return result
        try:
            if contract['budget'].get('coding_invocations', 1) > 1 and contract.get('iteration_policy_hash') != iteration.policy_hash(self):
                raise Conflict('有限迭代执行器已变化，请新建冻结方案，不能换实现续跑')
            # Each suite owns its split; new dev/final fact groups are disjoint.
            data, fingerprint = suite.frozen_data() if suite is not legacy else frozen_data()
            if suite.configuration() != contract['input']['content']['config']:
                raise Conflict('冻结数据、基线、评测器或环境已变化，拒绝换条件续跑')
            if suite is not legacy:
                stage('features', '准备冻结语义信号' + ('与 Luna 原文重排（单题最多120秒，结果缓存）' if suite is evidence else ''))
                data = suite.prepare_data(data, cancelled)
                if state.get('features_hash') and state['features_hash'] != data['features']['signals_hash']:
                    raise Conflict('冻结语义信号发生变化，拒绝换条件续跑')
                if suite is evidence:
                    self.preserve_evidence_usage(output, data)
                # Persist returned paid responses before a cancellation can reject the checkpoint.
                dump(output / 'semantic-signals.json', {'features': data['features'], 'signals': data['signals']})
                if suite is evidence:
                    dump(output / 'evidence-rerank.json', data['evidence_records'])
                state['features_hash'] = data['features']['signals_hash']; self.save(job, state)
            dump(output / 'contract.json', contract); dump(output / 'sources.json', data['corpus'])
            if not state.get('prepared'):
                if workspace.exists(): raise Conflict('工作目录已存在，拒绝覆盖未确认的代码')
                workspace.mkdir(parents=True)
                (workspace / 'ranker.py').write_text(baseline_code, encoding='utf-8')
                dump(workspace / 'corpus.json', data['corpus'])
                dump(workspace / 'dev.json', [q for q in data['questions'] if q['split'] == 'dev'])
                if suite is not legacy:
                    dump(workspace / 'dev-inputs.json', [signal for q, signal in zip(data['questions'], data['signals']) if q['split'] == 'dev'])
                    dump(workspace / 'dev-feedback.json', {})
                    (workspace / 'dev-eval.py').write_text(feedback.DEV_EVAL, encoding='utf-8')
                    (workspace / 'proposal.md').write_text('HYPOTHESIS: pending development diagnosis.\n', encoding='utf-8')
                    for name in ('candidate-first.json', 'candidate-revised.json'):
                        dump(workspace / name, {})
                (workspace / 'README.md').write_text('实现 rank(corpus, queries, top_k=10)，每个问题返回最多十个不同片段 id。只修改 ranker.py；标准库，无网络/依赖安装。dev.json 是开发集，正式测试由工作台保管。不要硬编码题目、标签或文档 ID。\n' + contract['input']['content']['note'], encoding='utf-8')
                if suite is not legacy:
                    (workspace / 'README.md').write_text('''混合检索排序实验。rank(corpus, queries, top_k=10) 返回每题最多十个不同原文片段 ID。
queries 的元素为 {text: 提问, dense: [{id: 原文片段 ID, score: BGE-M3 相似度}]}。
语义信号由已有固定版本 BGE-M3 计算：512 token窗口，取原文片段内最大相似度的前40项。
基线复用 FTS5 字段权重4/2/1及 RRF k=60；这是原文片段级排序实验，不包含线上预览/问答/核验。
只能修改 ranker.py 和 proposal.md；标准库、无网络、不安装依赖、不访问目录外文件。
dev-eval.py会自动将首候选和至多一次修订源码保存在两个candidate JSON槽中，不要手动修改槽。
宿主仅用开发集独立复算两个候选，Recall@5和MRR均不下降才可替换基线；按Recall再MRR选优，退步时保留此前最佳。
先读 dev-feedback.json：开发集逐题排名、语义命中位置、漏检原文摘录和URL。完整原文在 corpus.json。
Windows命令使用 python -X utf8 -I -B dev-eval.py 查看修改后的开发集反馈，-X utf8 不能仅用环境变量替代。
开发工具默认只输出指标、与基线的逐题变化和剩余失败；需要全部排名时加 --full。
只读检查最多2个命令，第3个命令前写入第一个通用候选。随后执行开发工具，至多再修订一次，最后写proposal并结束。
时间预算包含模型推理和工具往返；不需要先重测基线，dev-feedback.json已经是宿主实测。
不要写大段临时诊断脚本；使用已有诊断。-I下不能直接import ranker，开发工具已经正确加载该文件。
不要输出整个 corpus.json 或 dev-inputs.json；已有诊断含语义命中位置，无需重复编写临时诊断命令。
dev-eval.py、corpus.json、dev.json、dev-inputs.json均不可修改；只打印诊断，不要创建其他文件。
只根据开发集选择方案，测试标签与测试信号不向编码阶段开放。不得硬编码题目、答案、来源ID或开发标签映射。
在 proposal.md 用中文记录：开发失败依据、通用改法、预期收益（假设）、风险、开发前后指标、尚未验证部分。
最终工作台会独立评分，不能凭自报指标通过。不要把开发集改进写成测试集或线上收益。
''' + ('''\n本套件额外提供宿主根据问题+完整候选原文生成的无标签内容判断：
queries[i].evidence_ranking 是按问题支持度排序的最多10个ID，evidence_status为ok或fallback。
ok且空列表表示模型未找到支持原文；fallback表示调用/校验失败，已返回原基线前10。
这不是gold标签；仍可能错选或漏选，只能以开发题独立验证。请复用此信号，勿另写来源/邻段密度奖励。
模型调用及原文阅读成本由宿主单列，不能把缓存上的毫秒排序耗时写成端到端延迟。
''' if suite is evidence else '') + contract['input']['content']['note'], encoding='utf-8')
                for args in (['git', 'init', '-q'], ['git', 'add', '.'], ['git', '-c', 'user.name=ResearchAgent', '-c', 'user.email=local@researchagent.invalid', '-c', 'core.hooksPath=' + str(output / 'no-hooks'), 'commit', '-qm', 'Frozen baseline']):
                    subprocess.run(args, cwd=workspace, env=clean_environment(), capture_output=True, check=True, timeout=15)
                state['prepared'] = tree(); self.save(job, state)
            if not state.get('baseline'):
                if (workspace / 'ranker.py').read_text(encoding='utf-8') != baseline_code:
                    raise Conflict('基线代码发生变化')
                stage('baseline', '运行冻结检索基线')
                state['baseline'] = measured('baseline'); self.save(job, state)
            if suite is not legacy:
                # The baseline is the initial best.  This state is deliberately
                # JSON rather than a new DB column so old runs remain readable.
                development = state.setdefault('development', {})
                if not development.get('baseline'):
                    development['baseline'] = feedback.development_snapshot(
                        'baseline', state['baseline'], contract['baseline']['content']['config']['code_revision'])
                    development.setdefault('trials', [])
                    development['decision'] = feedback.select_best_development(
                        development['baseline'], development['trials'])
                    development['selected'] = development['decision']['selected']
                    dump(output / 'development-snapshots.json', development)
                    self.save(job, state)
            if suite is not legacy and not state.get('feedback_ready'):
                for name, value in feedback.development_files(data, state['baseline']).items():
                    dump(workspace / name, value); dump(output / name, value)
                (output / 'dev-eval.py').write_text(feedback.DEV_EVAL, encoding='utf-8')
                state['prepared'] = tree(); state['feedback_ready'] = True; self.save(job, state)
            if contract['budget'].get('coding_invocations', 1) > 1:
                iteration.run(self, job, contract, state, suite, workspace, output, data, private,
                              cancelled, stage, tree)
                current = tree()
            else:
                current = self._develop_round(job, contract, state, suite, workspace, output, data,
                                              private, cancelled, stage, tree)
            if not state.get('candidate'):
                if state.get('iteration'):
                    if state.get('final_evaluation_status') == 'started':
                        raise Conflict('最终测试已占用；未完成的终验不自动重放，请查看已保存失败')
                    state['final_evaluation_status'] = 'started'
                    self.save(job, state)
                stage('evaluating', '固定评测器测量候选；保留失败与无提升结果')
                candidate = measured('candidate')
                if sha(self.code_snapshot(workspace)) != state['code_hash']:
                    raise Conflict('候选执行期间修改了代码')
                if suite is not legacy and tree() != current:
                    raise Conflict('候选执行期间修改了工作目录')
                state['candidate'] = candidate
                if state.get('iteration'): state['final_evaluation_status'] = 'completed'
                self.save(job, state)
            stage('comparing', '写入实测版本并比较固定基线')
            result_id, baseline_id = self.commit(job, contract, state)
            comparison = self.records.compare_experiments(job['space_id'], baseline_id, result_id)
            selected_label = state.get('selected_label', 'candidate')
            selected_result = state['candidate']
            dump(output / 'result.json', {'result_version_id': result_id, 'baseline_version_id': baseline_id,
                                          'comparison': comparison, 'selection': state.get('development'),
                                          'candidate_metrics': state['candidate'].get('metrics'),
                                          'selected_label': selected_label,
                                          'selected_code_hash': state['code_hash'],
                                          'selected_metrics': selected_result.get('metrics'),
                                          'usage': state.get('usage', []), 'iteration': state.get('iteration')})
            self.store.finish(job['id'], 'completed', '受控实验完成。目标判定：' + comparison['outcome'] + '。实测版本、代码差异和运行轨迹已保存到「研究成果 → 实验与基线」。')
        except Exception as exc:
            failure = {'error': str(redact(str(exc)))[:2000], 'phase': state.get('phase'), 'at': now_iso()}
            dump(output / 'failure.json', failure)
            if not cancelled():
                state['failure'] = failure
                self.save(job, state)
                self.store.finish(job['id'], 'failed', '实验未完成；已保留日志和检查点。' + failure['error'], failure['error'])

    def _develop_round(self, job, contract, state, suite, workspace, output, data,
                       private, cancelled, stage, tree):
        """One receipt-bound coding turn and host development selection; never final grading."""
        baseline_code = suite.BASELINE
        active_round = state.get('iteration', {}).get('rounds', [{}])[-1]
        invoked = active_round.get('coding_invocations', 0) if state.get('iteration') else state['coding_invocations']
        if not state.get('code_hash'):
            receipt=self.coding_receipt(output) if invoked else None
            if invoked and receipt is None: raise Conflict('编码已调用过但未完成，请重试新尝试')
            if receipt is None:
                state['coding_invocations'] += 1
                if state.get('iteration'): active_round['coding_invocations'] = 1
                stage('coding', 'Codex 正在实现待验证假设' + (f"（第 {active_round['index']} 轮）" if state.get('iteration') else ''))
                env = clean_environment()
                key = os.environ.get('SUDOCODE_API_KEY', '')
                if not key: raise ValueError('缺少已配置的 SUDOCODE_API_KEY')
                env['RESEARCH_V4_MODEL_KEY'] = key
                def event(item):
                    item=json.loads(json.dumps(redact(item),ensure_ascii=False).replace(key,'[REDACTED]'))
                    with (output / 'events.jsonl').open('a', encoding='utf-8') as file:
                        file.write(json.dumps(item, ensure_ascii=False).replace(key, '[REDACTED]') + '\n')
                    if not cancelled() and item.get('type') in ('turn.completed', 'turn.failed', 'item.started'):
                        self.app.sessions.event(job, 'coding_agent', {**item, 'iteration': active_round.get('index', 1)})
                    if not cancelled() and item.get('type')=='turn.completed':
                        usage=normalize_usage(item.get('usage'))
                        usage['cache_read_tokens']=item.get('usage',{}).get('cached_input_tokens',usage['cache_read_tokens'])
                        self.app.sessions.event(job,'coding_usage',{'model':contract['model'],'aggregation':'codex_turn','usage':usage, 'iteration': active_round.get('index', 1)})
                prompt = ('Read README.md and ranker.py. Implement the hypothesis by modifying ONLY ranker.py. '
                          'corpus.json and dev.json are available for targeted inspection if necessary; never dump entire corpus files. '
                          'Preserve rank interface; use Python standard library. You may test on dev. Never access parent directories or secrets, '
                          'never change corpus/dev/git files, install packages or use network. Do not hardcode question or document ids. '
                          'Finish this single bounded coding attempt; report changes and risks, never invent measured results.\n'
                          + contract['input']['content']['note'])
                if suite is not legacy:
                    prompt = ('This is a local Python ranking task; all needed interface documentation is in README.md. '
                              'Read README.md, ranker.py and dev-feedback.json together with one shell command. '
                              'The feedback already includes lexical match counts, dense gold positions and failed source excerpts. '
                              'Use at most TWO read/diagnostic commands before writing your first generalizable candidate to ranker.py. '
                              'A negative development result is acceptable; the host retains the best development candidate or baseline. '
                              'Reuse the supplied FTS5/BGE-M3 baseline and frozen semantic inputs. Modify ONLY ranker.py and proposal.md. '
                              'Run python -X utf8 -I -B dev-eval.py before finalizing; inspect changed ranks, not only the average score. '
                              'On Windows always pass -X utf8 explicitly; use PowerShell here-strings for multiline Python rather than nested -c quotes. '
                              'After testing the first candidate, allow at most one development-driven revision, then test and finalize proposal.md. '
                              'Do not run parameter sweeps or write extra diagnostic scripts. Do not hardcode queries, gold labels or IDs. '
                              'Keep imports side-effect free and predictions deterministic. No new files, external reads, network or dependencies. '
                              'Write a short Chinese proposal with evidence, hypothesis, risks and actual development measurements; test performance is unknown. '
                              f"Finish within {contract['budget']['coding_seconds']} seconds and {contract['budget']['tool_calls']} tools. "
                              'Aim for 5-7 commands total, including edits and tests. Write a concrete first candidate after reading the supplied evidence.\n'
                              + contract['input']['content']['note'])
                prompt += contract.get('_iteration_prompt', '')
                coding = run_process(coding_command(workspace, private, contract['budget']['token_budget']),
                                     workspace, timeout=contract['budget']['coding_seconds'], cancelled=cancelled, stdin=prompt,
                                      env=env, event=event, tool_limit=contract['budget']['tool_calls'])
                raw_events = codex_events(coding['stdout'])
                coding['stdout']='\n'.join(json.dumps(e,ensure_ascii=False) for e in raw_events)
                coding['stderr']=str(redact(coding['stderr']))
                coding = json.loads(json.dumps(coding, ensure_ascii=False).replace(key, '[REDACTED]'))
                # Even cancellation, invalid files or missing slots must retain process evidence.
                dump(output / 'codex.json', coding)
                if coding['termination'] != 'completed': raise RuntimeError('Codex: ' + coding['termination'])
                completed_tree = tree()
                mutable = {'ranker.py', 'proposal.md', 'candidate-first.json', 'candidate-revised.json'} if suite is not legacy else {'ranker.py'}
                if set(completed_tree) != set(state['prepared']) or any(completed_tree[k] != value for k, value in state['prepared'].items() if k not in mutable):
                    raise ValueError('Codex 改动超出冻结文件范围，拒绝测量')
                coding['candidate_hash']=sha(self.code_snapshot(workspace))
                if suite is not legacy:
                    coding['candidate_slot_hashes'] = {name: completed_tree[name] for name in ('candidate-first.json', 'candidate-revised.json')}
                dump(output / 'codex.json', coding)
            events=self.coding_receipt(output)
            if events is None:raise RuntimeError('Codex 没有成功完成一轮')
            usage=[e['usage'] for e in events if e.get('type')=='turn.completed' and 'usage' in e]
            if sum(u.get('input_tokens',0)+u.get('output_tokens',0) for u in usage)>contract['budget']['token_budget']:
                raise RuntimeError('Codex 已报告用量超出编码 token 预算，停止后续执行')
            current = tree()
            mutable = {'ranker.py', 'proposal.md', 'candidate-first.json', 'candidate-revised.json'} if suite is not legacy else {'ranker.py'}
            if set(current) != set(state['prepared']) or any(current[k] != v for k, v in state['prepared'].items() if k not in mutable):
                raise ValueError('Codex 改动超出冻结文件范围，拒绝测量')
            code = self.code_snapshot(workspace)
            coding_receipt = json.loads((output/'codex.json').read_text(encoding='utf-8'))
            if sha(code)!=coding_receipt['candidate_hash']:
                raise Conflict('编码完成后代码被修改，不能归因于保存的 Codex 运行')
            if suite is legacy: compile(code, 'ranker.py', 'exec')
            (output / 'ranker.py').write_text(code, encoding='utf-8')
            (output / 'diff.patch').write_text(''.join(difflib.unified_diff(baseline_code.splitlines(True), code.splitlines(True), fromfile='baseline/ranker.py', tofile='candidate/ranker.py')), encoding='utf-8')
            if suite is not legacy:
                slot_hashes = coding_receipt.get('candidate_slot_hashes')
                names = ('candidate-first.json', 'candidate-revised.json')
                if not isinstance(slot_hashes, dict) or set(slot_hashes) != set(names):
                    raise Conflict('编码收据未固定开发候选快照，不能归因于保存的 Codex 运行')
                snapshots = []
                for name in names:
                    raw = self.candidate_slot_bytes(workspace, name)
                    if sha(raw) != slot_hashes[name]:
                        raise Conflict('编码完成后开发候选快照被修改，不能归因于保存的 Codex 运行')
                    snapshot = json.loads(raw.decode('utf-8'))
                    if snapshot == {}: continue
                    if not isinstance(snapshot, dict) or set(snapshot) != {'code', 'code_hash'} or not isinstance(snapshot['code'], str) or sha(snapshot['code']) != snapshot['code_hash']:
                        raise ValueError('开发候选快照格式或哈希无效')
                    if not any(s['code_hash'] == snapshot['code_hash'] for s in snapshots): snapshots.append(snapshot)
                if sha(code) != sha(baseline_code) and not any(s['code_hash'] == sha(code) for s in snapshots):
                    snapshots.append({'code': code, 'code_hash': sha(code)})
                if len(snapshots) > 2: raise ValueError('仅允许首候选和至多一次修订')
                state['development']['snapshots'] = snapshots
                state['development']['slot_hashes'] = slot_hashes
                (output / 'submitted-ranker.py').write_text(code, encoding='utf-8')
                (output / 'proposal.md').write_text((workspace / 'proposal.md').read_text(encoding='utf-8'), encoding='utf-8')
            state['code_hash'] = sha(code)
            state['usage'] = [e['usage'] for e in events if e.get('type') == 'turn.completed' and 'usage' in e]
            self.save(job, state)
        if suite is not legacy and not state.get('development_selected') and state.get('development', {}).get('snapshots'):
            # A restart can occur while a dev candidate is temporarily in
            # ranker.py. Restore the host-pinned submitted source before
            # normal checkpoint checks; arbitrary external edits still fail.
            current_hash = sha(self.code_snapshot(workspace))
            allowed = {state['code_hash'], sha(baseline_code),
                       *(s['code_hash'] for s in state['development']['snapshots'])}
            if current_hash not in allowed: raise Conflict('开发选优检查点代码发生未知修改')
            submitted = (output / 'submitted-ranker.py').read_text(encoding='utf-8')
            if sha(submitted) != state['code_hash']: raise Conflict('提交代码检查点哈希不一致')
            (workspace / 'ranker.py').write_text(submitted, encoding='utf-8')
        if sha(self.code_snapshot(workspace)) != state['code_hash']:
            raise Conflict('编码检查点与当前代码不一致，拒绝续跑')
        if suite is not legacy:
            current = tree()
            if set(current) != set(state['prepared']) or any(current[k] != value for k, value in state['prepared'].items() if k not in {'ranker.py', 'proposal.md', 'candidate-first.json', 'candidate-revised.json'}):
                raise Conflict('开发资料或评测工具发生变化，拒绝续跑')
            slot_hashes = state.get('development', {}).get('slot_hashes')
            if not isinstance(slot_hashes, dict) or any(current.get(name) != slot_hashes.get(name) for name in ('candidate-first.json', 'candidate-revised.json')):
                raise Conflict('编码完成后开发候选快照被修改，拒绝使用变更后的工作目录')
            if not state.get('development_selected'):
                development = state['development']
                snapshots = development.get('snapshots')
                if snapshots is None:
                    raise Conflict('缺少宿主固定的开发候选快照，拒绝从可修改的工作目录恢复')
                if len(snapshots) > 2: raise ValueError('仅允许首候选和至多一次修订')
                development['snapshots'] = snapshots
                stage('selecting', '仅用开发集复算候选，保留最佳版本；尚未对候选进行最终检验')
                trials = development.setdefault('trials', [])
                for index, snapshot in enumerate(snapshots, 1):
                    previous = next((t for t in trials if t['code_hash'] == snapshot['code_hash']), None)
                    if previous:
                        if previous['measurement_status'] == 'started':
                            previous.update(metrics={}, measurement_status='interrupted',
                                            measurement={'valid': False, 'error': 'development measurement interrupted; reserved attempt is not replayed'})
                            dump(output / 'development-snapshots.json', development); self.save(job, state)
                        continue
                    if state.get('development_evaluation_attempts', 0) >= 2 * contract['budget'].get('coding_invocations', 1):
                        raise Conflict('开发测量次数预算已耗尽')
                    code = snapshot['code']
                    (workspace / 'ranker.py').write_text(code, encoding='utf-8')
                    before = tree()
                    trial = {**snapshot, 'label': (f"round-{active_round['index']}-" if state.get('iteration') else '') + 'candidate-' + str(index), 'metrics': {}, 'measurement_status': 'started'}
                    trials.append(trial)
                    state['development_evaluation_attempts'] = state.get('development_evaluation_attempts', 0) + 1
                    dump(output / 'development-snapshots.json', development); self.save(job, state)
                    measured_dev = feedback.evaluate(workspace, feedback.development_data(data), cancelled, private)
                    if tree() != before: raise Conflict('候选执行期间修改了工作目录')
                    trial.update(feedback.development_snapshot(trial['label'], measured_dev, snapshot['code_hash']),
                                 measurement=measured_dev, measurement_status='completed')
                    development['decision'] = feedback.select_best_development(development['baseline'], trials)
                    development['selected'] = development['decision']['selected']
                    dump(output / 'development-snapshots.json', development); self.save(job, state)
                development['decision'] = feedback.select_best_development(development['baseline'], trials)
                development['selected'] = development['decision']['selected']
                selected = next((trial for trial in trials if trial['label'] == development['selected']), None)
                selected_code = selected['code'] if selected else baseline_code
                (workspace / 'ranker.py').write_text(selected_code, encoding='utf-8')
                (output / 'ranker.py').write_text(selected_code, encoding='utf-8')
                (output / 'diff.patch').write_text(''.join(difflib.unified_diff(baseline_code.splitlines(True), selected_code.splitlines(True), fromfile='baseline/ranker.py', tofile='selected/ranker.py')), encoding='utf-8')
                state['code_hash'] = sha(selected_code)
                state['selected_label'] = development['selected']
                state['development_selected'] = True
                dump(output / 'development-snapshots.json', development); self.save(job, state)
            current = tree()
        return tree()

    def commit(self, job, contract, state):
        with closing(self.store._connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            current = self.store._require(db, 'research_jobs', job['id'], job['space_id'])
            old = db.execute('SELECT result_version_id FROM experiment_runs WHERE job_id=?', (job['id'],)).fetchone()[0]
            if old and current['status']=='completed' and current['execution_generation']==job['execution_generation']:
                return old, db.execute('SELECT baseline_version_id FROM experiment_versions WHERE id=?', (old,)).fetchone()[0]
            if current['status'] != 'running' or current['execution_generation'] != job['execution_generation']:
                raise Conflict('实验已停止，拒绝写回结果')
            if old:
                return old, db.execute('SELECT baseline_version_id FROM experiment_versions WHERE id=?', (old,)).fetchone()[0]
            result_ids = []
            selected_label = state.get('selected_label', 'candidate')
            # candidate.json is the final independent measurement of the
            # development-selected code. Rejected code and dev measurements
            # remain in development-snapshots.json.
            selected_result = state['candidate']
            selection_note = ''
            if selected_label == 'baseline' and state.get('development'):
                selection_note = ' 开发集未找到更好的候选，已保留基线；负例保存在 development-snapshots.json。'
            for label, frozen in (('baseline', contract['baseline']), ('candidate', contract['input'])):
                latest = db.execute('SELECT id,version FROM experiment_versions WHERE experiment_id=? ORDER BY version DESC LIMIT 1', (frozen['experiment_id'],)).fetchone()
                if latest['id'] != frozen['id']: raise Conflict('实验档案已有新版本；本次产物保留，不覆盖新版本')
                content = json.loads(encoded(frozen['content']))
                measured = state[label] if label == 'baseline' else selected_result
                content.update(status='completed', results=measured['metrics'], measurement_status='WORKBENCH_MEASURED',
                               executed_by_workbench=True, result_source='experiment-run:' + job['id'] + '/' + label + '.json',
                               note='固定评测器实际执行；原方案 ' + frozen['id'] + selection_note)
                content['config']['code_revision'] = frozen['content']['config']['code_revision'] if label == 'baseline' or selected_label == 'baseline' else state['code_hash']
                if label == 'candidate' and state.get('development'):
                    content['selection'] = state['development']
                    if state.get('iteration'): content['iteration'] = state['iteration']
                result_id = uuid4().hex
                db.execute('INSERT INTO experiment_versions VALUES(?,?,?,?,?,?,?)', (result_id, frozen['experiment_id'], latest['version'] + 1,
                    None if label == 'baseline' else result_ids[0], encoded(content), digest(encoded(content)), now_iso()))
                self.records.event(db, job['space_id'], result_id, 'experiment_measured', {'job_id': job['id'], 'input_version_id': frozen['id']})
                result_ids.append(result_id)
            state['phase'] = 'completed'
            if state.get('failure'):state['recovered_from']=state.pop('failure')
            db.execute('UPDATE experiment_runs SET result_version_id=?,state=?,updated_at=? WHERE job_id=?',
                       (result_ids[1], encoded(state), now_iso(), job['id']))
            summary='受控实验已完成；固定基线、候选实测版本与运行轨迹已保存。目标是否达到请查看实验比较。'
            db.execute("UPDATE research_jobs SET status='completed',stage='completed',summary=?,error='',updated_at=? WHERE id=?",(summary,now_iso(),job['id']))
            self.store._message(db,job['conversation_id'],'assistant',summary,'EXPERIMENT',job['id'],job['model_id'],job['model_name'])
            return result_ids[1], result_ids[0]
