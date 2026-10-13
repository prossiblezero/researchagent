"""Same-budget short Auto Research before/after versus autonomous Codex screening."""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import types
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from evals.prepare_short_research import read, write as raw_write, digest
from evals.short_research_task import rank, score
from evals.run_evidence_qa_acceptance import instrument, sanitize_full
from research_agent import coding_tool
from research_agent import experiment_process as native
from research_agent.models import load_dotenv, model_from_env
from research_agent.trace import now_iso, redact
from research_agent.workbench import Workbench
from research_agent.workbench_store import WorkbenchStore

PROTECTED = ('dev.json', 'evaluate.py', 'TASK.md')
TERMINAL = {'completed', 'failed', 'cancelled', 'interrupted'}
RUNTIME = ROOT/'evals/short_research_task.py'


def write(path, value):
    raw_write(path, sanitize_full(value, os.environ.get('SUDOCODE_API_KEY', '')))


def private_paths(workspace, extra=()):
    workspace = workspace.resolve()
    extra = [p.resolve() for p in extra]
    if any(workspace.is_relative_to(p) or p.is_relative_to(workspace) for p in extra):
        raise ValueError('Scoring packets/logs must be separate from the editable workspace')
    paths = [ROOT/'datasets', ROOT/'research-notes', Path('D:/paper'), Path('D:/project/newagent'), *extra]
    # Deny saved other runs without denying the ancestor of this active workspace.
    parent = workspace.parent
    while parent.is_relative_to(ROOT/'experiments'):
        paths.extend(p for p in parent.iterdir() if not workspace.is_relative_to(p.resolve()))
        parent = parent.parent
    return list(dict.fromkeys(paths))


def task_text(task, protocol):
    return f'''Improve {task} evidence retrieval over the supplied BM25 baseline using development experiments.
You may inspect, plan, implement, run, diagnose, and retry autonomously within a TOTAL {protocol['wall_seconds']} seconds
and {protocol['total_reported_tokens']} reported input (including cached) plus output tokens. Controller and coding use the same pool.
No network, downloads, package installation, extra model calls, or reading outside this workspace. Python: {sys.executable}
Read TASK.md. Only development cases are available; the host will run your frozen method on unseen source groups.
Final deliverables: method.py (and optional .py helpers), submission.json with chosen_method ("method" or "baseline"),
dev_recall (number actually measured for that chosen method), hypothesis, and limitations. Only .py helpers transfer to final scoring;
put fixed learned constants in code. rank(query, documents, k) must return exactly k unique valid document IDs.
The goal is >=0.02 absolute held-out macro evidence recall improvement over BM25. Do not claim held-out success; labels are hidden.
Do not alter dev.json, evaluate.py or TASK.md. Use the supplied scorer, inspect individual errors, and choose your final method honestly.
Run baseline: evaluate.py --method baseline --output baseline.json
Run candidate: evaluate.py --output candidate.json
Both commands use the Python above. Output files contain host-compatible metrics/config plus predictions.
Native Windows sandbox startup may take about 30 seconds; allow 90 seconds per host experiment command.
For Auto Research: workspace is already prepared, no external research needed (research_calls=0); save a plan with evidence=[],
then use coding_agent/run_experiments as useful. No prescribed number of plans, revisions, or ablations.
Respect remaining_execution and the TOTAL deadline.
Codex on this Windows host may need PowerShell literal here-string or Python writes if apply_patch fails.

Shared method notes (hypotheses, not measured improvements): BM25 is the fixed lexical reference (k1=1.2, b=0.75),
using only document text. Title/section weighting, stopword removal, normalization and query expansion may help or hurt.
HotpotQA has two supporting documents; entity/title links may help recover a bridge document.
QASPER uses paragraphs from one paper; section and question type may matter.
LoCoMo has complete dialogue turns; neighboring turns, speakers and dates may matter. Gold answers are never input features.
References: Robertson & Zaragoza, The Probabilistic Relevance Framework, https://doi.org/10.1561/1500000019 ;
QASPER https://aclanthology.org/2021.naacl-main.365/ ; HotpotQA https://aclanthology.org/D18-1259/ ;
LoCoMo https://github.com/snap-research/locomo . These are shared source notes, not a live literature search.
'''


def stage(workspace, packets, task, protocol):
    workspace.mkdir(parents=True)
    shutil.copyfile(packets/task/'dev.json', workspace/'dev.json')
    shutil.copyfile(RUNTIME, workspace/'evaluate.py')
    (workspace/'method.py').write_text('from evaluate import rank\n', encoding='utf-8')
    text = task_text(task, protocol)
    (workspace/'TASK.md').write_text(text, encoding='utf-8', newline='\n')
    return text


def check_command_length(command):
    if os.name == 'nt' and len(subprocess.list2cmdline(command)) >= 30000:
        raise native.ProcessNotStarted('Sandbox permission command exceeds the Windows launch limit; no provider request started')


class Budget:
    def __init__(self, protocol, out):
        self.protocol, self.out = protocol, out
        self.models, self.cli = [], []
        self.started = time.monotonic()

    @property
    def usage_rows(self):
        return [r for m in self.models for r in m.usage_records] + [u for r in self.cli for u in r['usage']]

    @property
    def tokens(self):
        return sum(u[k] for u in self.usage_rows for k in ('input_tokens', 'output_tokens') if type(u.get(k)) is int and u[k] >= 0)

    @property
    def unknown(self):
        for row in self.usage_rows:
            if any(type(row.get(k)) is not int or row[k] < 0 for k in ('input_tokens', 'output_tokens')):
                return True
            if row.get('total_tokens') is not None and row['total_tokens'] != row['input_tokens'] + row['output_tokens']:
                return True
        return any(not r['usage'] for r in self.cli if r.get('finished') and not r.get('launch_not_attempted'))

    def left(self):
        return max(0, self.protocol['wall_seconds'] - (time.monotonic()-self.started))

    def stopped(self):
        return self.left() <= 0 or self.tokens >= self.protocol['total_reported_tokens'] or self.unknown

    def guard(self):
        if self.stopped():
            raise RuntimeError('Shared wall/token budget exhausted or provider usage unknown')

    def factory(self, **kwargs):
        folder = self.out/'models'/f'{len(self.models):03d}'
        folder.mkdir(parents=True)
        model = instrument(model_from_env('sudocode-luna'), folder,
                           {'max_model_attempts': 120, 'max_case_seconds': self.protocol['wall_seconds'],
                            'max_reported_tokens': self.protocol['total_reported_tokens']})
        model.request_deadline = self.started + self.protocol['wall_seconds']
        model.request_guard = self.guard
        self.models.append(model)
        return model

    def process(self, args, workspace, **kwargs):
        self.guard()
        kwargs['timeout'] = min(kwargs['timeout'], self.left())
        cancelled = kwargs.get('cancelled', lambda: False)
        kwargs['cancelled'] = lambda: cancelled() or self.stopped()
        kwargs['tool_limit'] = 1000
        cli = len(args) > 1 and args[1] == 'exec'
        if cli:
            row = {'usage': [], 'started_at': now_iso()}
            self.cli.append(row)
            callback = kwargs.get('event')
            def event(item):
                if item.get('type') in {'turn.completed', 'turn.failed'} and isinstance(item.get('usage'), dict):
                    row['usage'].append(item['usage'])
                if callback:
                    callback(item)
            kwargs['event'] = event
        try:
            check_command_length(args)
            receipt = native.run_process(args, workspace, **kwargs)
            if cli:
                row.update(receipt={k: receipt.get(k) for k in ('exit_code', 'termination', 'seconds', 'tool_calls')})
                write(self.out/f'cli-{len(self.cli):02d}.json', receipt)
            return receipt
        except native.ProcessNotStarted as exc:
            if cli:
                row.update(launch_not_attempted=True, error=str(redact(str(exc))))
            raise
        finally:
            if cli:
                row['finished'] = True
                write(self.out/'usage.json', self.summary())

    def summary(self):
        records = [r for m in self.models for r in m.usage_records]
        return {'seconds': round(time.monotonic()-self.started, 3), 'reported_tokens': self.tokens, 'completed': False,
                'controller_attempts': len(records), 'controller_records': records, 'cli': self.cli,
                'usage_complete': not self.unknown, 'cli_backend_request_count': None,
                'token_budget_valid': not self.unknown and self.tokens <= self.protocol['total_reported_tokens']}


def auto_run(out, packets, task, arm, protocol):
    budget = Budget(protocol, out)
    app = Workbench(WorkbenchStore(out/'state.sqlite'), budget.factory, None, out/'traces', start_worker=False)
    if arm == 'before':
        code = subprocess.check_output(['git', 'show', protocol['before']+':research_agent/auto_research.py'], cwd=ROOT).decode('utf-8')
        old = types.ModuleType('research_agent._short_before')
        old.__package__ = 'research_agent'
        exec(compile(code, 'auto_research_before.py', 'exec'), old.__dict__)
        app.auto_research = old.AutoResearch(app)
    sid = app.store.save_space({'name': task+'-'+arm})['id']
    cid = app.store.create_conversation(sid, task+'-'+arm)['id']
    limits = {'decisions': 120, 'coding_calls': 12, 'research_calls': 0, 'preparations': 0,
              'coding_seconds': protocol['wall_seconds'], 'coding_tokens': protocol['total_reported_tokens'],
              'experiment_seconds': protocol['wall_seconds'], 'experiment_model_calls': 0, 'experiment_model_seconds': 0}
    job = app.auto_research.enqueue(sid, cid, task_text(task, protocol), model_id='sudocode-luna', budget=limits)
    workspace = app.coding.work_root/job['id']
    stage(workspace, packets, task, protocol)
    check_command_length(native.coding_command(workspace, private_paths(workspace, (out, packets)), protocol['total_reported_tokens']))
    write(out/'workspace.json', {'path': str(workspace)})
    original_remaining, original_submit = app.coding.remaining, app.coding.submit
    def remaining(current):
        values = original_remaining(current)
        values['coding_tokens'] = min(values['coding_tokens'], max(0, protocol['total_reported_tokens']-budget.tokens))
        for name in ('coding_seconds', 'experiment_seconds'):
            values[name] = min(values[name], int(budget.left()))
        return values
    def submit(current, request_id, request):
        budget.guard()
        request = dict(request)
        request['protected_files'] = list(dict.fromkeys([*request.get('protected_files', []), *PROTECTED]))
        if not request.get('execution_only'):
            request['token_budget'] = min(request.get('token_budget', protocol['total_reported_tokens']-budget.tokens), protocol['total_reported_tokens']-budget.tokens)
            request['seconds'] = min(request.get('seconds', int(budget.left())), int(budget.left()))
        return original_submit(current, request_id, request)
    app.coding.remaining, app.coding.submit = remaining, submit
    app.coding.private_paths = lambda _: private_paths(workspace, (out, packets))
    budget.started = time.monotonic()
    try:
        with patch.object(coding_tool, 'run_process', budget.process):
            previous = None
            while True:
                current = app.store.job(sid, job['id'])
                if current['status'] in TERMINAL:
                    break
                if budget.stopped():
                    app.store.cancel(sid, job['id'])
                    break
                app.auto_research.tick()
                claimed = app.store.claim_next()
                if claimed:
                    app.execute(claimed)
                with closing(app.store._connect()) as db, db:
                    row = db.execute("SELECT id FROM coding_tasks WHERE status='queued' ORDER BY created_at LIMIT 1").fetchone()
                    if row:
                        db.execute("UPDATE coding_tasks SET status='running' WHERE id=?", (row['id'],))
                if row:
                    app.coding.execute(sid, row['id'])
                current = app.store.job(sid, job['id'])
                signature = (current['status'], current['stage'], current['recent_action'])
                if signature != previous:
                    print(json.dumps({'task': task, 'arm': arm, 'state': signature, 'tokens': budget.tokens,
                                      'seconds_left': round(budget.left())}, ensure_ascii=False), flush=True)
                    previous = signature
        current = app.store.job(sid, job['id'])
        write(out/'detail.json', app.auto_research.detail(sid, job['id']))
        write(out/'controller-state.json', app.auto_research.state(current))
        (out/'answer.md').write_text(current['summary'] or current['error'], encoding='utf-8')
        summary = budget.summary()
        summary.update(status=current['status'], completed=current['status']=='completed')
        return workspace, summary
    finally:
        app.close()
        write(out/'usage.json', budget.summary())


def direct_run(out, packets, task, protocol):
    workspace = ROOT/'experiments'/'short-runs'/digest(str(out).encode('utf-8'))[:16]
    prompt = stage(workspace, packets, task, protocol)
    write(out/'workspace.json', {'path': str(workspace)})
    budget = Budget(protocol, out)
    receipt = budget.process(native.coding_command(workspace, private_paths(workspace, (out, packets)), protocol['total_reported_tokens'],
                             readonly_paths=[workspace/p for p in PROTECTED]), workspace,
                             timeout=protocol['wall_seconds'], stdin=prompt,
                             env={**native.clean_environment(), 'RESEARCH_V4_MODEL_KEY': os.environ['SUDOCODE_API_KEY']})
    summary = budget.summary()
    events = native.codex_events(receipt['stdout'])
    terminal = [e for e in events if e['type'] in ('turn.completed', 'turn.failed')]
    completed = (receipt['exit_code'] == 0 and receipt['termination'] == 'completed' and terminal
                 and terminal[-1]['type'] == 'turn.completed' and not any(e['type']=='invalid_jsonl' for e in events))
    summary.update(status=receipt['termination'], completed=bool(completed))
    return workspace, summary


def independent_score(out, workspace, packets, task, protocol):
    """Copy regular code files, then execute in native sandbox; never import agent code in the host."""
    result = {'valid_submission': False}
    try:
        for name in PROTECTED:
            expected = (packets/task/'dev.json').read_bytes() if name=='dev.json' else (
                RUNTIME.read_bytes() if name=='evaluate.py' else task_text(task, protocol).encode('utf-8'))
            if coding_tool.regular_file(workspace, name, limit=64*1024*1024) != expected:
                raise ValueError('Protected input modified: '+name)
        submission = json.loads(coding_tool.regular_file(workspace, 'submission.json'))
        if (submission.get('chosen_method') not in ('method', 'baseline') or type(submission.get('dev_recall')) not in (int, float)
                or not math.isfinite(submission['dev_recall']) or not 0 <= submission['dev_recall'] <= 1):
            raise ValueError('Missing or invalid submission selection/measurement')
        source = {name: text for name, text in coding_tool.CodingTool.source_snapshot(workspace).items() if name.endswith('.py')}
        write(out/'frozen-source.json', source)
        write(out/'submission.json', submission)
        # A selected baseline must not import any submitted helper that shadows stdlib.
        scoring_source = source if submission['chosen_method']=='method' else {'evaluate.py': RUNTIME.read_bytes().decode('utf-8')}
        for split in ('dev', 'held_out'):
            final = ROOT/'experiments'/'short-scores'/digest(str(out).encode('utf-8'))[:16]/split
            final.mkdir(parents=True)
            for name, code in scoring_source.items():
                path = final/name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(code.encode('utf-8'))
            shutil.copyfile(RUNTIME, final/'evaluate.py')
            hashes = {name: digest((final/name).read_bytes()) for name in scoring_source}
            packet = read(packets/task/(split+'.json'))
            inputs = {**packet, 'cases': [{k: v for k, v in case.items() if k != 'gold'} for case in packet['cases']]}
            write(final/(split+'.json'), inputs)
            output_name = split+'-predictions.json'
            command = [sys.executable, '-B', 'evaluate.py', '--method', submission['chosen_method'],
                       '--input', split+'.json', '--output', output_name]
            command = native.sandbox_command(final, command, private_paths(final, (out, packets)),
                         readonly_paths=[*[final/name for name in scoring_source], final/(split+'.json')])
            check_command_length(command)
            receipt = native.run_process(command, final, timeout=120)
            write(out/(split+'-scoring-receipt.json'), receipt)
            actual = {name: digest(code.encode('utf-8')) for name, code in coding_tool.CodingTool.source_snapshot(final).items() if name.endswith('.py')}
            if actual != hashes:
                raise ValueError('Frozen submission source changed during independent scoring')
            if receipt['exit_code'] != 0 or receipt['termination'] != 'completed':
                raise ValueError('Independent scoring failed: '+str(receipt['termination']))
            prediction = json.loads(coding_tool.regular_file(final, output_name, limit=16*1024*1024))['predictions']
            write(out/(split+'-predictions.json'), prediction)
            measured = score(packet['cases'], prediction, packet['k'])
            baseline = score(packet['cases'], {c['id']: rank(c['query'], c['documents'], packet['k']) for c in packet['cases']}, packet['k'])
            result[split] = {'candidate': measured, 'baseline': baseline, 'gain': measured['recall_at_k']-baseline['recall_at_k']}
        result.update(valid_submission=True, dev_claim_error=abs(submission['dev_recall']-result['dev']['candidate']['recall_at_k']))
        result['unsupported_positive_dev_claim'] = (submission['dev_recall'] > result['dev']['baseline']['recall_at_k']+1e-6
                                                  and result['dev']['gain'] <= 0)
    except Exception as exc:
        result['error'] = str(redact(str(exc)))[:2000]
    write(out/'score.json', result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--packets', required=True, type=Path)
    parser.add_argument('--task', required=True, choices=['qasper', 'hotpotqa', 'locomo'])
    parser.add_argument('--arm', required=True, choices=['before', 'after', 'codex'])
    args = parser.parse_args()
    out, packets = args.output.resolve(), args.packets.resolve()
    if out.exists():
        raise ValueError('Never overwrite or silently retry an attempt')
    protocol = read(packets/'protocol.json')
    for split, expected in protocol['tasks'][args.task]['packets'].items():
        if digest((packets/args.task/(split+'.json')).read_bytes()) != expected:
            raise ValueError('Frozen task packet changed')
    out.mkdir(parents=True)
    load_dotenv(ROOT/'.env')
    write(out/'condition.json', {'protocol': protocol, 'task': args.task, 'arm': args.arm, 'started_at': now_iso(),
          'source_hashes': {p.relative_to(ROOT).as_posix(): digest(p.read_bytes()) for p in [RUNTIME, Path(__file__), * (ROOT/'research_agent').glob('*.py')]},
          'transport_difference': 'Controller uses Chat Completions with default reasoning; coding/direct use Responses via Codex CLI with medium reasoning.',
          'provider_alias': 'Configured third-party gpt-5.6-luna alias; underlying weights unverified.'})
    try:
        workspace, usage = direct_run(out, packets, args.task, protocol) if args.arm=='codex' else auto_run(out, packets, args.task, args.arm, protocol)
        write(out/'usage.json', usage)
        result = independent_score(out, workspace, packets, args.task, protocol)
        # A killed/incomplete attempt is retained, but cannot be a within-budget completed success.
        passed = (usage['completed'] and usage['seconds'] <= protocol['wall_seconds'] and usage['token_budget_valid'] and result['valid_submission']
                  and result['held_out']['gain'] >= protocol['minimum_gain'])
        write(out/'result.json', {'task': args.task, 'arm': args.arm, 'passed': passed, 'usage': usage, 'score': result})
        print(json.dumps({'task': args.task, 'arm': args.arm, 'passed': passed, 'tokens': usage['reported_tokens'],
                          'seconds': usage['seconds'], 'score': result}, ensure_ascii=False), flush=True)
    except Exception as exc:
        write(out/'failure.json', {'error': str(redact(str(exc))), 'type': type(exc).__name__})
        write(out/'result.json', {'task': args.task, 'arm': args.arm, 'passed': False,
              'usage': read(out/'usage.json') if (out/'usage.json').exists() else {'usage_complete': False},
              'score': {'valid_submission': False, 'error': str(redact(str(exc)))}})
        raise


if __name__ == '__main__':
    main()
