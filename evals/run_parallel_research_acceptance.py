"""Compare one/two workers using real controller decisions and original-source QA."""
from __future__ import annotations

import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from evals.run_evidence_qa_acceptance import instrument, model_condition, read, score, sha, validate_panel, write
from research_agent.models import load_dotenv, model_from_env
from research_agent.reports import render_report
from research_agent.trace import now_iso, redact
from research_agent.usage import summarize_usage
from research_agent.workbench import Workbench
from research_agent.workbench_store import WorkbenchStore


class UnsettledRun(RuntimeError):
    """Stop the suite when request workers have not actually terminated."""


def receipt_usage(folder):
    receipts = [read(p) for p in sorted((folder/'models').glob('*/requests/*.json'))]
    known = [r['usage']['total_tokens'] for r in receipts if (r.get('usage') or {}).get('total_tokens') is not None]
    return {'requests': len(receipts), 'reported_tokens': sum(known),
            'unknown_usage_attempts': len(receipts)-len(known),
            'incomplete_requests': sum('finished_at' not in r for r in receipts)}


def run_one(folder, case, tasks, docs, protocol, mode):
    folder.mkdir(parents=True, exist_ok=False)
    budget = protocol['budget']
    lock = threading.Lock()
    models, intervals, active = [], [], set()
    attempts = 0
    started = None

    def factory(**kwargs):
        nonlocal attempts
        with lock:
            index = len(models)
            model_folder = folder/'models'/f'{index:03d}'
            model_folder.mkdir(parents=True, exist_ok=False)
            model = instrument(model_from_env(protocol['model']), model_folder, {
                'max_model_attempts': budget['per_model_requests'],
                'max_case_seconds': budget['per_model_seconds'],
                'max_reported_tokens': budget['per_model_tokens']})
            original = model._complete
            def capped(messages, tools):
                nonlocal attempts
                with lock:
                    remaining = budget['wall_seconds_per_run'] - (time.monotonic() - started)
                    tokens = sum(u.get('total_tokens') or 0 for m in models for u in m.usage_records)
                    if app.stop.is_set() or attempts >= budget['total_requests_per_run'] or remaining <= 0 or tokens >= budget['reported_tokens_per_run']:
                        raise RuntimeError('Frozen paired-research budget exhausted')
                    attempts += 1
                    model._active_timeout = min(model.timeout, remaining)
                return original(messages, tools)
            model._complete = capped
            models.append(model)
            return model

    def no_web(*args, **kwargs):
        raise RuntimeError('Frozen local-source comparison forbids external search')

    store = WorkbenchStore(folder/'state.sqlite')
    app = Workbench(store, factory, no_web, folder/'traces', start_worker=False)
    app.coding.work_root = folder/'no-execution'
    app.coding.output_root = folder/'coding-tools'
    # Evaluator-only scheduling control: both arms expose the identical model tools.
    # The serial arm disables only the second research worker, not any agent capability.
    if mode == 'serial':
        app.parallel_worker = threading.Thread(target=lambda: None, name='serial-evaluation-placeholder', daemon=True)
    original_execute = app.execute
    def measured_execute(job):
        row = {'job_id': job['id'], 'kind': job['kind'], 'started_seconds': time.monotonic()-started}
        with lock:
            intervals.append(row)
            active.add(job['id'])
        try:
            return original_execute(job)
        finally:
            with lock:
                row['finished_seconds'] = time.monotonic()-started
                active.discard(job['id'])
    app.execute = measured_execute
    try:
        sid = store.save_space({'name': case['title'], 'download_root': r'D:\paper'})['id']
        chunk_map = {}
        for doc in docs:
            artifact, _ = app.library.save(sid, {
                'kind': 'paper' if doc['key'] in {'docling', 'react'} else 'document',
                'title': doc['title'], 'url': doc['url'], 'canonical_id': doc['key'],
                'metadata': doc['metadata'], 'data': None, 'warnings': [],
                'boundary': 'Frozen original public text; no model-written corpus',
                'chunks': [dict(c, text=c['content']) for c in doc['chunks']]})
            for chunk in app.library.chunks(sid, artifact['id']):
                chunk_map[str(chunk['id'])] = [doc['key'], chunk['ordinal']]
        app.retrieval.sync(sid)
        retrieval = app.retrieval.status(sid)
        if retrieval['mode'] != 'hybrid' or retrieval['coverage'] != 1:
            raise RuntimeError('Both arms require the same complete existing hybrid index before model calls')
        write(folder/'inputs.json', {'space_id': sid, 'chunk_map': chunk_map, 'retrieval': retrieval,
              'corpus_sha256': protocol['corpus_sha256'], 'question_ids': case['task_ids']})
        assigned = [{'question': '['+task['id']+'] '+task['query']+'\n'+protocol['question_suffix'],
                     'mode': 'local', 'effort': 'quick'} for task in tasks]
        goal = ('这是已授权的研究工作流调度验收，执行预算为0，只交付资料调研。'
                '下面两个子问题相互独立，请调用一次research_parallel，tasks逐字采用以下JSON数组（含题号与范围），'
                '不增加、改写或合并问题。两项返回后，根据实际报告/失败汇总并finish_research reported；'
                '有失败就明确保留缺口，不重复研究、不调用编码或实验。'
                '汇总引用各自子报告链接，不混用不同子任务的E编号，不补造未经原文核验的新事实。\n' +
                json.dumps(assigned, ensure_ascii=False))
        chat = store.create_conversation(sid, case['title'])['id']
        started = time.monotonic()
        parent = app.auto_research.enqueue(sid, chat, goal, model_id=protocol['model'], budget={
            'decisions': budget['parent_decisions'], 'research_calls': 2, 'coding_calls': 0,
            'preparations': 0, 'experiment_seconds': 1, 'experiment_model_calls': 0})
        write(folder/'goal.json', {'goal': goal, 'tasks': assigned, 'model': model_condition(models[0]),
                                 'mode': mode, 'research_workers': 1 if mode == 'serial' else 2})
        app.worker.start()
        while True:
            parent = store.job(sid, parent['id'])
            if parent['status'] in {'completed', 'failed', 'cancelled', 'interrupted'}:
                break
            if time.monotonic()-started >= budget['wall_seconds_per_run']:
                store.cancel(sid, parent['id'])
            write(folder/'progress.json', {'parent': {k: parent[k] for k in ('id', 'status', 'stage')},
                  'seconds': time.monotonic()-started, 'attempts': attempts})
            time.sleep(.5)
        delivered = time.monotonic()-started
        # Include post-delivery memory calls in costs; do not cut off request receipts.
        settle_deadline = max(time.monotonic(), started+budget['wall_seconds_per_run'])+30
        while time.monotonic() < settle_deadline:
            with lock:
                if not active:
                    break
            time.sleep(.2)
        with lock:
            unsettled = sorted(active)
        if unsettled:
            raise UnsettledRun('Request workers remain active after the run budget: '+', '.join(unsettled))
        settled = time.monotonic()-started
        detail = app.auto_research.detail(sid, parent['id'])
        write(folder/'detail.json', detail)
        write(folder/'intervals.json', intervals)
        write(folder/'parent.json', parent)
        (folder/'report.md').write_text(parent['summary'] or parent['error'], encoding='utf-8')
        (folder/'report.html').write_text(render_report(parent['summary'] or parent['error'], case['title']), encoding='utf-8')
        rows = []
        children = [store.job(sid, c['id']) for c in detail['research']]
        batches = [json.loads(c['payload']).get('auto_batch') for c in children]
        protocol_match = (len(children) == 2 and all(batches) and len(set(batches)) == 1
                          and all(c['kind'] == 'LOCAL_QA' and c['research_effort'] == 'quick' for c in children))
        for task, question in zip(tasks, assigned):
            matches = [c for c in children if c['question'] == question['question']]
            if len(matches) != 1:
                protocol_match = False
                rows.append({'id': task['id'], 'product_completed': False, 'error': 'Expected exact assigned question missing or duplicated'})
                continue
            child = matches[0]
            child_folder = folder/task['id']
            run = store.get(child['run_id']) if child['run_id'] else None
            with closing(store._connect()) as db:
                events = [dict(r) for r in db.execute('SELECT * FROM conversation_events WHERE job_id=? ORDER BY id', (child['id'],))]
            for event in events:
                event['payload'] = json.loads(event['payload'])
            timing = next((r for r in intervals if r['job_id'] == child['id']), None)
            duration = timing.get('finished_seconds', settled)-timing['started_seconds'] if timing else 0
            row = score(task, mode, child, run, events, [], chunk_map, duration)
            row['execution_started'] = timing is not None
            row.pop('usage')  # Request receipts account for the whole run once, below.
            rows.append(row)
            write(child_folder/'job.json', child)
            write(child_folder/'run.json', run)
            write(child_folder/'events.json', events)
            write(child_folder/'score.json', row)
            (child_folder/'answer.md').write_text(child['summary'] or child['error'], encoding='utf-8')
            write(child_folder/'review.json', {'answer_sha256': row['answer_sha256'], 'facts': [
                {'id': fact['id'], 'correct': None, 'supported': None, 'citation_sufficient': None,
                 'actual_citations': [], 'reason': ''} for fact in task['facts']],
                'task_pass': None, 'notes': 'Manual original-source review pending; keyword checks are prefilters.'})
        child_intervals = [r for r in intervals if r['kind'] != 'AUTO_RESEARCH' and 'finished_seconds' in r]
        overlap = max(0, min(r['finished_seconds'] for r in child_intervals)-max(r['started_seconds'] for r in child_intervals)) if len(child_intervals) == 2 else 0
        with closing(store._connect()) as db:
            coding = db.execute('SELECT count(*) FROM coding_tasks').fetchone()[0]
        metrics = {'mode': mode, 'case': case['id'], 'parent_status': parent['status'], 'outcome': detail['outcome'],
                   'delivery_seconds': delivered, 'settled_seconds': settled, 'unsettled_jobs': unsettled,
                   'actual_child_overlap_seconds': overlap, 'rows': rows, 'coding_tasks': coding,
                   'completed_children': sum(r['product_completed'] for r in rows),
                   **receipt_usage(folder), 'assignment_and_budget_matched': protocol_match,
                   'usage': summarize_usage([u for m in models for u in m.usage_records]),
                   'model_selected_parallel_tool': any(json.loads(c['payload']).get('auto_batch') for c in store.jobs(sid) if c['id'] != parent['id']),
                   'manual_semantic_review_pending': True}
        metrics['functional_passed'] = (parent['status'] == 'completed' and detail['outcome'] == 'reported'
            and protocol_match and len(rows) == 2 and metrics['completed_children'] == 2 and coding == 0 and not unsettled
            and (overlap > 0 if mode == 'parallel' else overlap == 0))
        write(folder/'metrics.json', metrics)
        return metrics
    finally:
        app.close()
        # Error exports must also wait for in-flight receipts before the next arm starts.
        close_deadline = time.monotonic()+max((m.timeout for m in models), default=0)+10
        while time.monotonic() < close_deadline:
            with lock:
                if not active:
                    break
            time.sleep(.2)
        with lock:
            if active:
                raise UnsettledRun('Stopped suite with unresolved workers; receipts may still be incomplete: '+', '.join(sorted(active)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--case-file', type=Path, default=ROOT/'datasets/project/parallel-research-v1.json')
    args = parser.parse_args()
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    protocol = read(args.case_file)
    panel_path, corpus_path = (ROOT/protocol[k] for k in ('panel_source', 'corpus_source'))
    if sha(panel_path) != protocol['panel_sha256'] or sha(corpus_path) != protocol['corpus_sha256']:
        raise ValueError('Frozen original-source inputs changed')
    panel, docs = read(panel_path), read(corpus_path)
    validate_panel(panel, docs)
    tasks = {t['id']: t for t in panel['cases']}
    if type(protocol.get('repeats')) is not int or not 1 <= protocol['repeats'] <= 3:
        raise ValueError('Comparison needs one to three complete repetitions')
    cases = protocol.get('cases')
    if not isinstance(cases, list) or not 1 <= len(cases) <= 4 or len({c['id'] for c in cases}) != len(cases):
        raise ValueError('Comparison needs one to four uniquely named case pairs')
    for case in cases:
        ids = case.get('task_ids')
        if not isinstance(ids, list) or len(ids) != 2 or len(set(ids)) != 2 or not all(i in tasks for i in ids):
            raise ValueError('Each pair needs two distinct existing original-source questions')
    load_dotenv(ROOT/'.env')
    os.environ.setdefault('BGE_MODEL_PATH', str(ROOT/'data/models/bge-m3'))
    source_manifest = {p.relative_to(ROOT).as_posix(): sha(p) for p in [*ROOT.glob('research_agent/*.py'), Path(__file__).resolve(), args.case_file.resolve()]}
    write(out/'condition.json', {'started_at': now_iso(), 'protocol': protocol, 'source_sha256': source_manifest})
    write(out/'panel.json', panel)
    write(out/'sources.json', docs)
    runs = []
    abort_suite = False
    for repeat in range(protocol['repeats']):
        for index, case in enumerate(protocol['cases']):
            modes = ['serial', 'parallel'] if (repeat+index) % 2 == 0 else ['parallel', 'serial']
            for mode in modes:
                if any(sha(ROOT/name) != value for name, value in source_manifest.items()):
                    raise RuntimeError('Product changed during frozen paired comparison')
                name = f'{repeat+1}-{case["id"]}-{mode}'
                try:
                    result = run_one(out/name, case, [tasks[i] for i in case['task_ids']], docs, protocol, mode)
                    runs.append({'name': name, 'repeat': repeat, **result})
                except Exception as exc:
                    failure = {'name': name, 'repeat': repeat, 'mode': mode, 'case': case['id'], 'functional_passed': False,
                               'error': type(exc).__name__+': '+str(redact(str(exc))), **receipt_usage(out/name)}
                    write(out/name/'failure.json', failure)
                    runs.append(failure)
                    abort_suite = isinstance(exc, UnsettledRun)
                write(out/'progress.json', {'runs': runs})
                print(json.dumps({'run': name, 'passed': runs[-1]['functional_passed'],
                                  'seconds': runs[-1].get('delivery_seconds'), 'error': runs[-1].get('error')}, ensure_ascii=False), flush=True)
                if abort_suite:
                    break
            if abort_suite:
                break
        if abort_suite:
            break
    pairs = []
    for repeat in range(protocol['repeats']):
        for case in protocol['cases']:
            by_mode = {r['mode']: r for r in runs if r['case'] == case['id'] and r['repeat'] == repeat}
            both = all(by_mode.get(m, {}).get('functional_passed', False) for m in ('serial', 'parallel'))
            row = {'repeat': repeat, 'case': case['id'], 'both_functional_passed': both}
            if all('delivery_seconds' in by_mode.get(m, {}) for m in ('serial', 'parallel')):
                a, b = (by_mode[m]['delivery_seconds'] for m in ('serial', 'parallel'))
                row.update(serial_seconds=a, parallel_seconds=b, reduction_fraction=1-b/a)
            pairs.append(row)
    summary = {'runs': runs, 'pairs': pairs, 'functional_passed_runs': sum(r['functional_passed'] for r in runs),
               'total_runs': len(runs), 'aborted_unsettled': abort_suite, 'manual_semantic_review_pending': True,
               'scope': protocol['scope'], 'source_files_unchanged': all(sha(ROOT/n) == h for n, h in source_manifest.items())}
    timed = [p for p in pairs if 'serial_seconds' in p]
    if timed:
        summary['all_timed_pairs_reduction_fraction'] = 1-sum(p['parallel_seconds'] for p in timed)/sum(p['serial_seconds'] for p in timed)
    comparable = [p for p in timed if p['both_functional_passed']]
    if comparable:
        summary['successful_matched_pairs_reduction_fraction'] = 1-sum(p['parallel_seconds'] for p in comparable)/sum(p['serial_seconds'] for p in comparable)
    summary['costs_including_failures'] = {key: sum(r.get(key, 0) for r in runs) for key in
        ('requests', 'reported_tokens', 'unknown_usage_attempts', 'incomplete_requests')}
    summary['expected_runs'] = protocol['repeats']*len(cases)*2
    write(out/'metrics.json', summary)
    (out/'report.md').write_text('# 双路并行真实调研对照\n\n'+protocol['purpose']+'\n\n'+
        '\n'.join(f'- {p["case"]} repeat{p["repeat"]+1}: {json.dumps(p, ensure_ascii=False)}' for p in pairs)+
        '\n\n所有首次运行及失败均保留。原文逐事实人工复核尚待完成，功能完成不替代事实正确性。\n', encoding='utf-8')
    return 0 if summary['functional_passed_runs'] == len(runs) == summary['expected_runs'] and summary['source_files_unchanged'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
