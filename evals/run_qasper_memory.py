"""Original QASPER questions in three-session paper sequences, two memory arms."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import re
import statistics
import sys
import tarfile
import time
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from evals.run_memory_learning_benchmark import (
    BUDGET, STRATEGY, KeywordMemory, configure, qa,
)
from research_agent.library import Library
from research_agent.models import model_from_env
from research_agent.workbench import Workbench
from research_agent.workbench_store import WorkbenchStore

OUT = ROOT / 'evals/reports/qasper-memory-20261013'
ARMS = ('keyword', 'integrated')
NO_MEMORY_BASELINE = False
SOURCE = 'https://qasper-dataset.s3.us-west-2.amazonaws.com/qasper-train-dev-v0.3.tgz'
SOURCE_HASH = 'a28fdf966db827bcee3d873107d6b6669864fb7ca8fbf73a192f5e39191bdb5a'
EVALUATOR = 'https://raw.githubusercontent.com/allenai/qasper-led-baseline/main/scripts/evaluator.py'
PAPERS = ['1903.11437', '1709.10367', '1909.03405', '1911.02855', '1907.01468',
          '1909.09587', '2002.04181', '1605.03481', '1909.09534', '2002.08795']
INSTRUCTIONS = '''仅使用当前研究区的本地论文与先前研究记忆，不联网。
Answer the original question in English using the specified paper. You may reuse relevant findings and procedural experience from earlier sessions, and can always read the full saved paper.
Begin with a single line: Answer: <a concise direct answer, without citations or explanation on that line>.
For a yes/no question that line should say Yes or No; if the paper does not answer the question, say Unanswerable. Then give a short supporting explanation with citations to the original passages using the provided [E#] identifiers. Do not invent evidence or question details.'''


def source_manifest():
    paths = [*sorted((ROOT / 'research_agent').glob('*.py')), Path(__file__),
             ROOT / 'evals/run_memory_learning_benchmark.py', ROOT / 'evals/run_evidence_qa_acceptance.py']
    return {str(p.relative_to(ROOT)): qa.sha(p) for p in paths}


def prepare():
    if (OUT / 'protocol.json').exists():
        return
    OUT.mkdir(parents=True, exist_ok=True)
    raw = urlopen(SOURCE, timeout=45).read()
    assert hashlib.sha256(raw).hexdigest() == SOURCE_HASH
    with tarfile.open(fileobj=io.BytesIO(raw), mode='r:gz') as archive:
        name = next(n for n in archive.getnames() if n.endswith('qasper-dev-v0.3.json'))
        data = json.load(archive.extractfile(name))
    used = {t['source_id'] for t in qa.read(ROOT / 'datasets/open/qasper/tasks.json')}
    if NO_MEMORY_BASELINE:
        used.update(qa.read(ROOT / 'evals/reports/qasper-memory-20261013/papers-and-gold.json'))
    eligible = [pid for pid, paper in data.items() if len(paper['qas']) >= 3 and pid not in used]
    eligible.sort(key=lambda pid: hashlib.sha256(('20261013:' + pid).encode()).hexdigest())
    if not NO_MEMORY_BASELINE:
        assert eligible[:10] == PAPERS
    selected = {pid: {**data[pid], 'qas': data[pid]['qas'][:3]} for pid in eligible[:10]}
    assert sum(len(p['qas']) for p in selected.values()) == 30
    qa.write(OUT / 'papers-and-gold.json', selected)
    code = urlopen(EVALUATOR, timeout=30).read()
    (OUT / 'official_evaluator.py').write_bytes(code)
    qa.write(OUT / 'protocol.json', {
        'created_at': qa.now_iso(), 'source': SOURCE, 'source_sha256': SOURCE_HASH,
        'evaluator_source': EVALUATOR, 'evaluator_sha256': hashlib.sha256(code).hexdigest(),
        'data_sha256': qa.sha(OUT / 'papers-and-gold.json'), 'source_manifest': source_manifest(),
        'model': qa.model_condition(model_from_env('default')), 'budget': BUDGET,
        'instructions': INSTRUCTIONS,
        'paper_ids': list(selected), 'questions': 30, 'runs': 60, 'followup_questions': 20,
        'excluded_paper_ids': sorted(used),
        'arms': {ARMS[0]: ('No cross-task memory: a fresh source-only database for every question, with finding/procedure extraction disabled.' if NO_MEMORY_BASELINE else 'Original keyword history, no automatic finding/procedure extraction.'),
                 'integrated': 'Hybrid history, ordinary findings, and procedural learning.'},
        'common': 'Same current Agent, hybrid source retrieval, original full papers, prompt, model and budget. Passage reading and compact verification are shared by both arms.',
        'sequence': 'New conversation per question. Original published question order; first question warmup, next two are primary evaluation. Integrated memory persists within a paper; no_memory starts a new source-only database on every question.',
        'scoring': 'Unmodified official Answer F1 and Evidence F1, text_evidence_only=True. Missing outputs score zero. Benchmark gold never enters agent prompts or memory.',
        'boundary': 'QASPER-derived project memory evaluation, not an official memory leaderboard or cross-paper skill transfer test.',
    })
    print(json.dumps({'prepared': 30, 'papers': 10, 'model': model_from_env('default').name}), flush=True)


def evaluator():
    spec = importlib.util.spec_from_file_location('qasper_official', OUT / 'official_evaluator.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def short_answer(answer):
    # Parsing is independent of reference answers; keep an audit flag for format misses.
    match = re.search(r'(?im)^\s*(?:\*\*)?Answer\s*:(?:\*\*)?\s*([^\r\n]+)', answer)
    text = match.group(1) if match else next((p for p in answer.split('\n\n') if p.strip()), '')
    text = re.sub(r'\[(?:E|S)\d+\]', '', text).strip().strip('*').strip()
    return text, bool(match)


def seed(paper_id, paper, arm):
    folder = OUT / 'chains' / paper_id / arm
    if (folder / 'seed.json').exists():
        return WorkbenchStore(folder / 'state.sqlite'), qa.read(folder / 'seed.json')
    store = WorkbenchStore(folder / 'state.sqlite')
    spaces = store.spaces()
    sid = spaces[0]['id'] if spaces else store.save_space({'name': paper['title'][:100]})['id']
    chunks = []
    for section in paper['full_text']:
        chunks.append({'text': section['section_name'] or '', 'section': section['section_name'], 'page': None})
        for text in section['paragraphs']:
            chunks.append({'text': text, 'section': section['section_name'], 'page': None})
    chunks.append({'text': paper['abstract'], 'section': 'Abstract', 'page': None})
    for figure in paper.get('figures_and_tables', []):
        chunks.append({'text': figure['caption'], 'section': 'Figure/table caption (image not provided)', 'page': None})
    chunks = [c for c in chunks if c['text'].strip()]
    for c in chunks:
        c.update(line_start=1, line_end=c['text'].count('\n') + 1)
    lib = Library(store)
    item, _ = lib.save(sid, {'kind': 'paper', 'title': paper['title'],
        'url': 'https://arxiv.org/abs/' + paper_id, 'canonical_id': 'qasper:' + paper_id,
        'metadata': {'benchmark': 'QASPER', 'paper_id': paper_id}, 'data': None,
        'warnings': [], 'boundary': 'Original public QASPER paper; figures are captions only.', 'chunks': chunks})
    info = {'space_id': sid, 'chunk_map': {}, 'original_paragraphs': {}}
    for c in lib.chunks(sid, item['id']):
        info['chunk_map'][str(c['id'])] = [paper_id, c['ordinal']]
        info['original_paragraphs'][str(c['id'])] = chunks[c['ordinal']]['text']
    qa.write(folder / 'seed.json', info)
    return store, info


def run_chain(entry):
    index, (paper_id, paper) = entry
    expected_model = qa.read(OUT / 'protocol.json')['model']
    for arm in ARMS if index % 2 == 0 else ARMS[::-1]:
        for position, question in enumerate(paper['qas']):
            qid = question['question_id']
            folder = OUT / 'cases' / qid / arm
            if (folder / 'result.json').exists():
                continue
            assert not (folder / 'started.json').exists(), 'Inspect an interrupted first attempt before resuming: ' + str(folder)
            store, info = seed(paper_id, paper, arm + '-' + qid if arm == 'no_memory' else arm)
            folder.mkdir(parents=True, exist_ok=True)
            models = []
            def factory(model_id='default'):
                model = model_from_env('default')
                assert qa.model_condition(model) == expected_model
                models.append(qa.instrument(model, folder, BUDGET))
                return models[-1]
            work = Workbench(store, factory, None, folder / 'traces', start_worker=False)
            work.retrieval.sync(info['space_id'])
            status = work.retrieval.status(info['space_id'])
            with closing(store._connect()) as db:
                total, indexed = db.execute("SELECT count(*),sum(status='indexed') FROM retrieval_index_state WHERE space_id=? AND corpus='documents'", (info['space_id'],)).fetchone()
            assert status['mode'] == 'hybrid' and total == indexed and total > 0, status
            if arm == 'keyword':
                work.retrieval = KeywordMemory(work.retrieval, store, work.library)
            if arm in ('keyword', 'no_memory'):
                work.memory.extract = lambda *a, **kw: []
                work.memory.extract_procedure = lambda *a, **kw: None
            sid = info['space_id']
            if arm == 'no_memory':
                assert not store.jobs(sid) and not work.memory.list(sid)
                assert all(a['kind'] == 'paper' for a in work.library.list(sid))
            chat = store.create_conversation(sid, question['question'])['id']
            prompt = INSTRUCTIONS + '\nPaper: ' + paper['title'] + '\nQuestion: ' + question['question']
            store.message(sid, chat, 'user', prompt)
            job = store.enqueue(sid, chat, prompt, prompt, [], research_effort='quick', kind='LOCAL_QA')
            qa.pin_arm(store, job, arm + '-' + qid, STRATEGY)
            qa.write(folder / 'started.json', {'at': qa.now_iso(), 'paper_id': paper_id, 'position': position + 1})
            before = time.perf_counter()
            work.execute(store.claim_next())
            job = store.job(sid, job['id'])
            case = {'id': qid, 'category': 'warmup' if position == 0 else 'followup', 'facts': [],
                    'unanswerable': all(a['answer']['unanswerable'] for a in question['answers'])}
            row = qa.export_attempt(folder, case, arm, store, info, job, time.perf_counter() - before,
                                    [r for m in models for r in m.usage_records])
            answer = job.get('summary') or ''
            predicted, formatted = short_answer(answer)
            run = qa.read(folder / 'run.json') or {}
            cited = set(re.findall(r'\[E(\d+)\]', answer))
            evidence = list(dict.fromkeys(info['original_paragraphs'][str(e['provenance']['chunk_id'])]
                for e in run.get('evidence', []) if e['evidence_id'][1:] in cited
                and str(e.get('provenance', {}).get('chunk_id')) in info['original_paragraphs']))
            events = qa.read(folder / 'events.json')
            row.update(paper_id=paper_id, question=question['question'], position=position + 1,
                       prediction={'answer': predicted, 'evidence': evidence}, answer_format_ok=formatted,
                       procedure_count=sum(len(e['payload']['items']) for e in events if e['kind'] == 'procedure_memory_loaded'),
                       finding_count=sum(len(e['payload']['items']) for e in events if e['kind'] == 'research_memory_loaded'),
                       memory=work.memory.list(sid))
            official = evaluator()
            gold = official.get_answers_and_evidence({paper_id: {**paper, 'qas': [question]}}, True)
            row['official'] = official.evaluate(gold, {qid: row['prediction']} if predicted else {})
            qa.write(folder / 'result.json', row)
            print(json.dumps({'paper': paper_id, 'position': position + 1, 'arm': arm,
                'answer_f1': row['official']['Answer F1'], 'evidence_f1': row['official']['Evidence F1'],
                'complete': row['product_completed'], 'procedures': row['procedure_count'],
                'seconds': round(row['seconds'], 1)}, ensure_ascii=False), flush=True)


def report():
    rows = [qa.read(p) for p in (OUT / 'cases').glob('*/*/result.json')]
    summary = []
    for phase in ('followup', 'warmup'):
        for arm in ARMS:
            group = [r for r in rows if r['arm'] == arm and r['category'] == phase]
            if not group:
                continue
            known_tokens = [r['usage']['total_tokens'] for r in group if r['usage']['total_tokens'] is not None]
            summary.append({'phase': phase, 'arm': arm, 'n': len(group),
                'answer_f1': statistics.mean(r['official']['Answer F1'] for r in group),
                'evidence_f1': statistics.mean(r['official']['Evidence F1'] for r in group),
                'completed': sum(r['product_completed'] for r in group),
                'mean_seconds': statistics.mean(r['seconds'] for r in group),
                'mean_tokens': statistics.mean(known_tokens) if known_tokens else None,
                'mean_tool_calls': statistics.mean(r['tool_calls'] or 0 for r in group),
                'mean_read_calls': statistics.mean(r['original_read_calls'] for r in group),
                'procedure_loaded_runs': sum(r['procedure_count'] > 0 for r in group),
                'finding_loaded_runs': sum(r.get('finding_count', 0) > 0 for r in group),
                'usage_complete_runs': sum(r['usage']['total_tokens_coverage'] == 1 for r in group),
                'format_ok_runs': sum(r['answer_format_ok'] for r in group)})
    qa.write(OUT / 'metrics.json', {'runs': len(rows), 'summary': summary})
    lines = ['# QASPER 连续论文研究记忆对照', '',
        '10 篇新选论文，各取 3 道公开原题。首题用于完整组积累经历，后两题为主要比较对象；每题独立会话，各组记忆保留规则见下。', '',
        ('A：无跨任务记忆，每题从只有原论文的独立环境开始。B：完整记忆，复用历史、相关事实及过程经验。' if NO_MEMORY_BASELINE else 'A：原关键词历史记忆。B：混合历史检索、事实与过程经验。') + '模型、原文检索、阅读与核验逻辑、提示、预算相同。', '',
        '官方 Answer F1 / Evidence F1（text_evidence_only）按实际输出计算；不是任务正确率。成本包含该题结束后的记忆提炼；不含一次性资料建索引。公开标准答案不进入 Agent 或记忆。', '',
        '|阶段|记忆方案|已运行|产品完成|答案 F1|证据 F1|平均 token|平均秒|平均工具/阅读次数|',
        '|---|---|---:|---:|---:|---:|---:|---:|---:|']
    for r in summary:
        tokens = f"{r['mean_tokens']:,.0f}" if r['mean_tokens'] is not None else '未报告'
        lines.append(f"|{r['phase']}|{r['arm']}|{r['n']}|{r['completed']}|{r['answer_f1']:.2%}|{r['evidence_f1']:.2%}|{tokens}|{r['mean_seconds']:.1f}|{r['mean_tool_calls']:.1f} / {r['mean_read_calls']:.1f}|")
    baseline_name = '无记忆' if NO_MEMORY_BASELINE else '关键词'
    lines += ['', '## 后续问题逐题结果', '', f'|论文 / 原题 ID|{baseline_name}答案 F1|完整答案 F1|{baseline_name}证据 F1|完整证据 F1|', '|---|---:|---:|---:|---:|']
    by_id = {}
    for r in rows:
        if r['category'] == 'followup':
            by_id.setdefault(r['id'], {})[r['arm']] = r
    pairs = []
    for qid, arms in by_id.items():
        if len(arms) != 2:
            continue
        a, b = arms[ARMS[0]], arms['integrated']
        pairs.append({'id': qid, 'paper_id': a['paper_id'], 'question': a['question'],
                      'answer_f1_delta': b['official']['Answer F1'] - a['official']['Answer F1'],
                      'evidence_f1_delta': b['official']['Evidence F1'] - a['official']['Evidence F1']})
        lines.append(f"|{a['paper_id']} / {qid}|{a['official']['Answer F1']:.2%}|{b['official']['Answer F1']:.2%}|{a['official']['Evidence F1']:.2%}|{b['official']['Evidence F1']:.2%}|")
    qa.write(OUT / 'pairs.json', pairs)
    lines += ['', '## 解释范围', '',
        '本次是基于 QASPER 原题的项目连续问答评测。相同论文的问题不一定依赖先前任务，不能据此声称跨领域方法迁移。证据 F1 检查标注段落重合，不单独证明每条引用的语义充分性。', '',
        '[条件与来源](protocol.json) · [逐题变化](pairs.json) · [指标](metrics.json)']
    (OUT / 'report.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    print(json.dumps({'runs': len(rows), 'summary': summary}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--prepare', action='store_true')
    parser.add_argument('--run', action='store_true')
    parser.add_argument('--report', action='store_true')
    parser.add_argument('--no-memory-baseline', action='store_true')
    args = parser.parse_args()
    if args.no_memory_baseline:
        NO_MEMORY_BASELINE = True
        OUT = ROOT / 'evals/reports/qasper-memory-nomemory-20261013'
        ARMS = ('no_memory', 'integrated')
        INSTRUCTIONS = INSTRUCTIONS.replace('and can always read the full saved paper', 'and should read relevant original passages to support the answer')
        INSTRUCTIONS = INSTRUCTIONS.replace('without citations or explanation on that line', 'include source citations but no extra explanation on that line')
    if args.prepare:
        prepare()
    if args.run:
        configure()
        protocol = qa.read(OUT / 'protocol.json')
        assert protocol['source_manifest'] == source_manifest(), 'Code changed after preparation'
        assert protocol['data_sha256'] == qa.sha(OUT / 'papers-and-gold.json')
        assert protocol['evaluator_sha256'] == qa.sha(OUT / 'official_evaluator.py')
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(run_chain, enumerate(qa.read(OUT / 'papers-and-gold.json').items())))
        report()
    elif args.report:
        report()
