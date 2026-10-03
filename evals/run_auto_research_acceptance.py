"""Isolated functional acceptance; preserve native receipts and real model route outputs."""
from __future__ import annotations

import argparse
import hashlib
import json
import time
import zipfile
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from evals.run_evidence_qa_acceptance import instrument, model_condition, read, write
from evals.auto_research_assessment import assessment_evidence, measurements_before
from research_agent.experiment_process import run_process, sandbox_command
from research_agent.models import load_dotenv, model_from_env
from research_agent.research_project import prepare, python_for
from research_agent.trace import now_iso, redact
from research_agent.workbench import OfflineRouter, Workbench, route_intent
from research_agent.workbench_store import WorkbenchStore
from research_agent.reports import render_report

DATA = ROOT / 'datasets/project/auto-research-functional-v1.json'
READING_DATA = ROOT / 'datasets/project/auto-research-reading-v1.json'
ITERATION_DATA = ROOT / 'datasets/project/auto-research-iteration-v1.json'
MODEL_LIMITS = {'max_model_attempts': 30, 'max_case_seconds': 600, 'max_reported_tokens': 400000}


def feedback_received(model_inputs, tasks):
    ids = {task['id'] for task in tasks}
    seen, measured = set(), set()
    for messages in model_inputs:
        for message in messages:
            if message.get('role') != 'tool':
                continue
            try:
                value = json.loads(message['content'])['UNTRUSTED_TOOL_DATA']
            except (KeyError, TypeError, ValueError):
                continue
            if not isinstance(value, dict) or value.get('id') not in ids:
                continue
            seen.add(value['id'])
            if any(m.get('valid') is True and m.get('metrics') for m in value.get('measurements', [])):
                measured.add(value['id'])
    return {'model_received_coding_feedback': bool(seen),
            'model_received_experiment_results': bool(measured)}


def method_iteration_evidence(requests, tasks, plans):
    """Trace measured feedback -> new method plan -> comparable rerun, not just task counts."""
    from research_agent.auto_research import AutoResearch
    by_id = {t['id']: t for t in tasks}
    by_call = {p['call_id']: p for p in plans}
    rows = []
    def task_plan(task):
        return json.loads(task['request']['plan'])
    def measured(task):
        return [m for m in AutoResearch.measurements_for_comparison(task) if m.get('valid') is True]
    for request_index, request in enumerate(requests):
        if request.get('purpose') != 'auto_research':
            continue
        response = request.get('response') or {}
        for decision in [response, *response.get('queued_tool_calls', [])]:
            proposal = by_call.get(decision.get('call_id'))
            if decision.get('tool_name') != 'save_plan' or not proposal:
                continue
            for message in request.get('messages', []):
                if message.get('role') != 'tool':
                    continue
                try:
                    feedback = json.loads(message['content'])['UNTRUSTED_TOOL_DATA']
                except (KeyError, TypeError, ValueError):
                    continue
                if not isinstance(feedback, dict) or feedback.get('id') not in by_id:
                    continue
                prior = by_id[feedback['id']]
                old_plan = task_plan(prior)
                if (old_plan['version'] >= proposal['version'] or
                        old_plan['method'].strip() == proposal['method'].strip()):
                    continue
                originals = measured(prior)
                # Match metrics to saved host measurements, not a model's rewritten summary.
                previous_rows = [m for m in measurements_before(requests, tasks, request_index)
                                 if m['plan_version'] == old_plan['version']]
                feedback_role = 'candidate' if any(m['role'] == 'candidate' for m in [*originals, *previous_rows]) else 'baseline'
                seen = [m for m in originals if m['role'] == feedback_role and any(
                    f.get('valid') is True and f.get('name') == m['name'] and f.get('metrics') == m['metrics']
                    for f in feedback.get('measurements', []))]
                for later in tasks:
                    if task_plan(later)['version'] != proposal['version']:
                        continue
                    after = later['state'].get('after', {})
                    if not after:
                        continue
                    before = prior['state'].get('after', {})
                    changed = {path for path in set(before) | set(after) if before.get(path) != after.get(path)}
                    if not any(Path(path).suffix == '.py' for path in changed):
                        continue  # A rewritten plan, docs or role rename is not implemented iteration.
                    protected_changed = {path for path in changed
                                         if path == 'evaluate.py' or path.startswith('data/') or path.startswith('data\\')}
                    allow_method_script_change = not protected_changed and 'evaluate.py' not in before and 'evaluate.py' not in after
                    candidates = [m for m in measured(later) if m['role'] == 'candidate' and any(
                        AutoResearch.comparable(m, old, require_script=not allow_method_script_change) for old in seen)]
                    if not candidates:
                        continue
                    all_rows = [m for task in tasks for m in measured(task)]
                    baselines = AutoResearch.baseline_matches(candidates[0], all_rows)
                    if len(baselines) != 1:
                        continue
                    peers = [m for m in all_rows if m['plan_version'] == proposal['version']
                             and AutoResearch.comparable(m, candidates[0])
                             and (bool(m.get('comparison_contract')) or m['source_revision'] == candidates[0]['source_revision'])]
                    if not any(m['role'] == 'ablation' for m in peers):
                        continue
                    comparisons = [(m['task_id'], m) for m in [*baselines, *peers]]
                    key = (prior['id'], proposal['version'], later['id'])
                    if any((r['prior_task'], r['new_plan_version'], r['rerun_task']) == key for r in rows):
                        continue
                    rows.append({'prior_task': prior['id'], 'prior_plan_version': old_plan['version'],
                        'new_plan_version': proposal['version'], 'plan_call_id': proposal['call_id'], 'rerun_task': later['id'],
                        'request_hash': request.get('request_hash'), 'metrics_seen_before_revision': [m['metrics'] for m in seen],
                        'new_candidate_metrics': [m['metrics'] for m in candidates],
                        'comparison_tasks': sorted({task_id for task_id, _ in comparisons}),
                        'same_evaluator_and_data_config': True,
                        'source_changes': sorted(k for k in before.keys() | after.keys() if before.get(k) != after.get(k)),
                        'manual_method_and_scoring_review_required': True})
    return {'structural_path_completed': bool(rows), 'iterations': rows,
            'scope': 'Real host results in a save_plan request followed by a comparable experiment; method meaning and scoring require separate review.'}


def native(out, data):
    store = WorkbenchStore(out/'state.sqlite')
    app = Workbench(store, OfflineRouter, None, out/'traces', start_worker=False)
    try:
        space = store.save_space({'name': 'Native preparation acceptance', 'download_root': 'D:\\paper'})['id']
        chat = store.create_conversation(space, 'Native environment')['id']
        app.auto_research.enqueue(space, chat, 'Validate isolated wheel dependency preparation; do not call a model', budget={'coding_calls': 0})
        job = store.claim_next()
        write(out/'job.json', job)
        prepared = prepare(app, job, {'sources': [], 'packages': data['native_environment']['packages']}, 'native-environment')
        write(out/'preparation.json', prepared)
        result = {'passed': False, 'model_calls': 0, 'preparation_ok': prepared['ok']}
        if prepared['ok']:
            workspace = app.coding.work_root/job['id']
            script = workspace/'verify_environment.py'
            script.write_text('import json\nimport packaging\nfrom packaging.version import Version\nprint(json.dumps({"version":packaging.__version__,"comparison":Version("1.9") < Version("2.0")}))\n', encoding='utf-8')
            command = sandbox_command(workspace, [python_for(app, job['id']), '-I', str(script)], [out], [script])
            execution = run_process(command, workspace, timeout=45)
            write(out/'execution.json', execution)
            value = json.loads(execution['stdout']) if execution['exit_code'] == 0 else {}
            result.update(passed=execution['exit_code'] == 0 and value == {'version': '25.0', 'comparison': True},
                          actual=value, script_sha256=hashlib.sha256(script.read_bytes()).hexdigest())
        store.finish(job['id'], 'completed' if result['passed'] else 'failed', json.dumps(result, ensure_ascii=False))
        write(out/'result.json', result)
        return result['passed']
    finally:
        app.close()


def routing(out, data, model_id):
    load_dotenv(ROOT/'.env')
    rows = []
    for case in data['routing']:
        folder = out/case['id']
        folder.mkdir()
        model = model_from_env(model_id)
        model = instrument(model, folder, {'max_model_attempts': 3, 'max_case_seconds': 100, 'max_reported_tokens': 50000})
        row = {'id': case['id'], 'question': case['question'], 'allowed': case['allowed'], 'model': model_condition(model), 'passed': False}
        try:
            model.usage_purpose = 'routing_acceptance'
            route = route_intent(model, [{'role': 'user', 'content': case['question']}], {'name': 'Agent research', 'description': '', 'local_material_count': 2})
            row.update(route=route, passed=route['intent'] in case['allowed'])
        except Exception as exc:
            row['error'] = str(redact(str(exc)))
        write(folder/'result.json', row)
        rows.append(row)
        print(json.dumps({'case': case['id'], 'passed': row['passed']}, ensure_ascii=False), flush=True)
        if row.get('error') and sum(bool(r.get('error')) for r in rows) >= 2:
            break  # Retain service failures; do not spend the entire panel on an unavailable provider.
    write(out/'result.json', {'rows': rows, 'passed': sum(r['passed'] for r in rows), 'attempted': len(rows), 'planned': len(data['routing'])})
    return len(rows) == len(data['routing']) and all(r['passed'] for r in rows)


def reading(out, data, model_id):
    from main import build_search
    from research_agent import HttpReader, ResearchAgent
    from research_agent.usage import summarize_usage
    load_dotenv(ROOT/'.env')
    models = []
    def factory(**kwargs):
        folder = out/'models'/f'{len(models):03d}'
        folder.mkdir(parents=True)
        model = instrument(model_from_env(model_id), folder, MODEL_LIMITS)
        models.append(model)
        return model
    def agent(observer, **kwargs):
        def observed(event):
            observer(event)
            if event['event'] in {'tool_call_requested', 'answer_check_started', 'answer_checked', 'run_finished'}:
                print(json.dumps({k: event.get(k) for k in ('event', 'name', 'ready', 'repair_round') if k in event}), flush=True)
        return ResearchAgent(build_search(), factory(), out/'traces', reader=HttpReader(), on_event=observed)
    store = WorkbenchStore(out/'state.sqlite')
    app = Workbench(store, factory, agent, out/'traces', start_worker=False)
    case = data['reading']
    started = time.monotonic()
    try:
        space = store.save_space({'name': case['id'], 'download_root': 'D:\\paper'})['id']
        chat = store.create_conversation(space, 'Original reading acceptance')['id']
        parent = app.auto_research.enqueue(space, chat, case['question'], model_id=model_id, budget={'coding_calls': 0})
        parent = store.claim_next()
        state = app.auto_research.state(parent)
        app.auto_research.dispatch(parent, state, {'tool_name': 'research', 'call_id': 'reading-acceptance',
            'arguments': {'question': case['question'], 'mode': 'web', 'effort': case['effort']}},
            json.loads(parent['payload'])['auto_research'])
        app.auto_research.yield_job(parent, state, waiting=True)
        job = store.claim_next()
        write(out/'job.json', job)
        write(out/'model.json', model_condition(factory()))
        app.execute(job)
        child = store.job(space, job['id'])
        result = app.auto_research.research_result(parent, child['id'])
        write(out/'child-result.json', result)
        run = store.get(child['run_id']) if child['run_id'] else None
        if run:
            write(out/'run.json', run)
        events = [json.loads(line) for line in Path(run['trace_path']).read_text(encoding='utf-8').splitlines()] if run else []
        checks = [e for e in events if e['event'] == 'answer_checked']
        reads = [e for e in events if e['event'] == 'tool_call_requested' and e.get('name') == 'read_evidence']
        metrics = {'job_status': child['status'], 'run_status': run['status'] if run else None,
                   'termination': run['termination'] if run else child['error'],
                   'answer_verification_ready': bool(checks and checks[-1]['ready']),
                   'seconds': round(time.monotonic()-started, 3),
                   'tool_calls': run['tool_calls'] if run else 0, 'read_evidence_calls': len(reads),
                   'requested_window_sizes': [e['args'].get('max_chars', 1800) for e in reads],
                   'usage': summarize_usage([r for m in models for r in m.usage_records]),
                   'whole_auto_research_accepted': False, 'scope': case['scope']}
        metrics['passed'] = child['status'] == 'completed' and metrics['answer_verification_ready']
        write(out/'metrics.json', metrics)
        write(out/'verification.json', checks)
        report = child['summary'] or child['error']
        (out/'report.md').write_text(report, encoding='utf-8')
        (out/'report.html').write_text(render_report(report, case['id']), encoding='utf-8')
        app.auto_research.tick()
        settled_parent = store.claim_next()
        if not settled_parent or settled_parent['id'] != parent['id']:
            raise RuntimeError('Reading acceptance parent did not become resumable')
        store.finish(parent['id'], 'completed', 'Reading-only acceptance finished; no coding or whole research evaluation.')
        return metrics['passed']
    finally:
        app.close()


def reading_checkpoint(source, data):
    """Load the latest saved draft from an original run or a failed recovery."""
    import sqlite3
    source = source.resolve()
    question = data['reading']['question']
    if (source/'recovery-source.json').is_file():
        origin = read(source/'recovery-source.json')
        prior = read(source/'run.json')
        if (read(source/'metrics.json').get('passed') is not False or prior['status'] == 'ok' or
                read(source/'cases.json')['reading']['question'] != question or
                data['reading'].get('source_job', origin['child_id']) != origin['child_id']):
            raise ValueError('Recovery requires the recorded failed reading question')
        raw = (source/'state-after.json').read_bytes()
        config, child_id = origin['original_config'], origin['child_id']
    else:
        with sqlite3.connect((source/'state.sqlite').as_uri()+'?mode=ro', uri=True) as db:
            db.row_factory = sqlite3.Row
            if (source/'run.json').is_file():
                prior = read(source/'run.json')
                child = read(source/'child-result.json')
            else:
                child = dict(db.execute('SELECT * FROM research_jobs WHERE id=?',
                                        (data['reading']['source_job'],)).fetchone() or {})
                if not child:
                    raise ValueError('Campaign reading job not found')
                prior = read(source/'research'/child['id']/'run.json')
            if prior['question'] != question or child['status'] != 'failed':
                raise ValueError('Recovery requires the recorded failed reading question')
            row = db.execute("SELECT payload FROM conversation_checkpoints WHERE job_id=? AND kind='execution' AND invalidated=0 ORDER BY id DESC LIMIT 1", (child['id'],)).fetchone()
        if not row:
            raise ValueError('No valid source checkpoint')
        raw, child_id = row[0].encode(), child['id']
        config = json.loads(Path(prior['trace_path']).read_text(encoding='utf-8').splitlines()[0])['config']
    state = json.loads(raw)
    if ((state.get('pending_decision') or {}).get('kind') != 'final' and
            not (state.get('revision_pending') and state.get('draft_answer'))):
        raise ValueError('Reading recovery requires a saved final draft')
    return state, config, {'path': str(source), 'child_id': child_id,
        'checkpoint_sha256': hashlib.sha256(raw).hexdigest(), 'original_config': config,
        'verification_question': question,
        'scope': 'Real ResearchAgent checkpoint verification/repair; no parent controller or Workbench postprocessing replay'}


def reading_recovery_agent(out, state, config, origin, model):
    """Bind a recovered draft to a private snapshot of its original library and scope."""
    import re
    import sqlite3
    from contextlib import closing
    from main import build_search
    from research_agent import HttpReader, ResearchAgent
    from research_agent.library import Library
    from research_agent.retrieval import Retriever
    source, seen = Path(origin['path']).resolve(), set()
    while not (source/'state.sqlite').is_file():
        if source in seen or len(seen) >= 16:
            raise ValueError('Cyclic or overlong reading recovery source chain')
        seen.add(source)
        previous = read(source/'recovery-source.json')
        if previous['child_id'] != origin['child_id']:
            raise ValueError('Reading recovery source changed child job')
        source = Path(previous['path']).resolve()
    database = source/'state.sqlite'
    with closing(sqlite3.connect(database.as_uri()+'?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        job = dict(db.execute('SELECT * FROM research_jobs WHERE id=?', (origin['child_id'],)).fetchone() or {})
        if (not job or job['question'] != origin['verification_question'] or job['status'] != 'failed'
                or job['kind'] not in {'RESEARCH', 'LOCAL_QA'}):
            raise ValueError('Recovery database does not contain the recorded failed reading job')
        conversation = db.execute('SELECT space_id FROM conversations WHERE id=? AND deleted_at IS NULL',
                                  (job['conversation_id'],)).fetchone()
        if not conversation or conversation['space_id'] != job['space_id']:
            raise ValueError('Recovery job conversation does not belong to its research space')
        target = out/'state.sqlite'
        with target.open('xb'):  # Never overwrite an earlier recovery or its source.
            pass
        with closing(sqlite3.connect(target)) as copy:
            db.backup(copy)
    snapshot_hash = hashlib.sha256(target.read_bytes()).hexdigest()
    store = WorkbenchStore(target)
    local_only = job['kind'] == 'LOCAL_QA'
    agent = ResearchAgent(None if local_only else build_search(), model, out/'traces', reader=HttpReader(),
        **{k: config[k] for k in ('max_tool_calls', 'max_rounds', 'max_context_tokens',
            'output_reserve_tokens', 'context_safety_margin_tokens') if k in config},
        retrieval=Retriever(store, Library(store)), space_id=job['space_id'], conversation_id=job['conversation_id'],
        allow_external=not local_only, resume_state=state, semantic_compaction=True,
        state_callback=lambda value: write(out/'state-after.json', value))
    if not local_only and job.get('search_scope') == 'arxiv':
        from research_agent.search import ArxivSearch
        agent.search = ArxivSearch(agent.search)
    agent.turn_seq = job['turn_seq']
    agent.verification_question = origin['verification_question']
    agent.harness_strategy = config.get('harness_strategy') or {}
    agent.allow_workspace_recall = not bool(re.search(r'(?:仅|只)(?:限于|用|使用|依据|基于|根据)?(?:当前|本|这个)会话|不要跨会话|不跨会话', job['question']))
    agent.allow_cross_session = agent.allow_workspace_recall and not bool(re.search(r'(?:仅|只)(?:限于|用|使用|依据|基于|根据)?(?:当前|本|这个)研究区|不要跨研究区|不跨研究区', job['question']))
    write(out/'recovery-environment.json', {'source_database': str(database), 'snapshot_sha256': snapshot_hash,
        'job_id': job['id'], 'kind': job['kind'], 'space_id': agent.space_id, 'conversation_id': agent.conversation_id,
        'turn_seq': agent.turn_seq, 'allow_external': agent.allow_external,
        'allow_workspace_recall': agent.allow_workspace_recall, 'allow_cross_session': agent.allow_cross_session,
        'harness_strategy': agent.harness_strategy, 'workers_started': False,
        'scope': 'Checkpoint verification and repair with original library scope; no parent or Workbench replay.'})
    return agent


def recover_reading(out, data, model_id, source, request_timeout=None):
    from dataclasses import asdict
    from main import build_search
    from research_agent import HttpReader, ResearchAgent
    from research_agent.usage import summarize_usage
    state, config, origin = reading_checkpoint(source, data)
    write(out/'state-before.json', state)
    write(out/'recovery-source.json', origin)
    load_dotenv(ROOT/'.env')
    model = model_from_env(model_id)
    if request_timeout is not None:
        model.timeout = request_timeout
    model = instrument(model, out, MODEL_LIMITS)
    stream_events = []
    model.stream_callback = stream_events.append
    write(out/'model.json', model_condition(model))
    print(json.dumps({'recovery_model': model.name}), flush=True)
    agent = reading_recovery_agent(out, state, config, origin, model)
    started = time.monotonic()
    result = agent.run(state['question'])
    write(out/'run.json', asdict(result))
    events = [json.loads(line) for line in Path(result.trace_path).read_text(encoding='utf-8').splitlines()]
    checks = [e for e in events if e['event'] == 'answer_checked']
    metrics = {'status': result.status, 'termination': result.termination,
        'answer_verification_ready': bool(checks and checks[-1]['ready']),
        'new_tool_calls': sum(e['event'] == 'tool_call_requested' for e in events),
        'seconds': round(time.monotonic()-started, 3), 'usage': summarize_usage(model.usage_records),
        'whole_auto_research_accepted': False}
    metrics['passed'] = result.status == 'ok' and metrics['answer_verification_ready']
    write(out/'metrics.json', metrics)
    write(out/'stream-events.json', stream_events)
    write(out/'verification.json', checks)
    (out/'report.md').write_text(result.answer, encoding='utf-8')
    (out/'report.html').write_text(render_report(result.answer, 'Original reading recovery'), encoding='utf-8')
    return metrics['passed']


def stage_initial_source(workspace, artifact):
    from research_agent.research_project import write_input
    from research_agent.coding_tool import relative_path
    if artifact.stat().st_size > 16 * 1024 * 1024:
        raise ValueError('Initial source snapshot exceeds 16 MiB')
    raw = artifact.read_bytes()
    sources = json.loads(raw)
    if not isinstance(sources, dict) or not sources or len(sources) > 2000 or len(raw) > 16 * 1024 * 1024:
        raise ValueError('Initial source must be a bounded nonempty code snapshot')
    for name, content in sources.items():
        relative_path(name)
        if (Path(name).suffix not in {'.py', '.md', '.toml', '.yaml', '.yml'} or
                any(part.lower() == '.venv' for part in Path(name).parts) or not isinstance(content, str)):
            raise ValueError('Initial source may contain only project code and documentation')
    hashes = {}
    for name, content in sources.items():
        encoded = content.encode('utf-8')
        write_input(workspace, name, encoded)
        hashes[name] = hashlib.sha256(encoded).hexdigest()
    return {'artifact': str(artifact.resolve()), 'artifact_sha256': hashlib.sha256(raw).hexdigest(),
            'source_sha256': hashes, 'note': 'Prior generated code only; no previous measurements count as new results.'}


def stage_campaign_inputs(workspace, case):
    """Stage a case's frozen public inputs; keep the legacy SciFact case unchanged."""
    inputs = case.get('inputs')
    if inputs is None:
        original = Path('D:/paper/researchagent-benchmarks/raw/scifact/data')
        expected = read(ROOT/'datasets/open/catalog.json')['scifact']['raw_sha256']
        records, staged = [], {}
        for name in ('claims_train.jsonl', 'claims_dev.jsonl', 'corpus.jsonl'):
            source = original / name
            raw = source.read_bytes()
            digest = hashlib.sha256(raw).hexdigest()
            if digest != expected[name]:
                raise ValueError('Original SciFact input changed: ' + name)
            target = workspace / 'data/scifact' / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
            staged[name] = digest
            records.append({'source': str(source), 'destination': f'data/scifact/{name}',
                            'sha256': digest, 'bytes': len(raw)})
        return {'workspace': str(workspace), 'inputs': records,
                'task_setting': case.get('provenance', 'legacy SciFact case')}
    if not isinstance(inputs, list) or not inputs:
        raise ValueError('campaign inputs must be a nonempty list')
    staged = {}
    records = []
    for item in inputs:
        if not isinstance(item, dict) or set(item) != {'source', 'destination', 'sha256'}:
            raise ValueError('each campaign input needs source/destination/sha256')
        source = Path(item['source'])
        if source.is_absolute():
            raise ValueError('campaign input source must be project-relative')
        source = (ROOT / source).resolve()
        if not source.is_file() or not source.is_relative_to(ROOT.resolve()):
            raise ValueError('campaign input source is outside the project')
        destination = Path(item['destination'])
        if destination.is_absolute() or '..' in destination.parts:
            raise ValueError('campaign input destination must be project-relative')
        raw = source.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        if digest != item['sha256']:
            raise ValueError('campaign input changed: ' + str(item['source']))
        target = workspace / destination
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        staged[str(destination).replace('\\', '/')] = digest
        records.append({'source': str(source), 'destination': str(destination).replace('\\', '/'),
                        'sha256': digest, 'bytes': len(raw)})
    return {'workspace': str(workspace), 'inputs': records, 'task_setting': case.get('provenance', 'project-authored case')}


def multi_seed_evidence(measurements, expected):
    """Require every valid role measurement to declare the same complete seed set."""
    expected = list(expected or [])
    if len(expected) < 2 or len(set(expected)) != len(expected):
        return {'required': expected, 'passed': False, 'reason': 'case requires at least two unique seeds'}
    rows = []
    for measurement in measurements:
        config = (measurement.get('result') or {}).get('config') or {}
        seeds = config.get('seeds')
        valid = isinstance(seeds, list) and seeds == expected
        rows.append({'name': measurement.get('name'), 'role': measurement.get('role'), 'seeds': seeds, 'valid': valid})
    passed = bool(rows) and all(row['valid'] for row in rows) and {'baseline', 'candidate', 'ablation'} <= {row['role'] for row in rows}
    return {'required': expected, 'passed': passed, 'rows': rows}


def campaign_metric_contract(case):
    """Pin public inputs before any generated code or measurement can run."""
    if case.get('audit_dataset') == 'locomo':
        inputs = {item['destination']: item['sha256'] for item in case['inputs']}
        return {'name': 'locomo_qa_v1', 'tasks_path': 'data/locomo/tasks.json',
                'tasks_sha256': inputs['data/locomo/tasks.json'], 'corpus_path': 'data/locomo/corpus.json',
                'corpus_sha256': inputs['data/locomo/corpus.json'], 'labels_sha256': case['labels_sha256'],
                'frozen_sources': case['frozen_sources'], 'seeds': list(case['seeds'])}
    if case.get('audit_dataset') != 'qasper':
        return None
    inputs = {item['destination']: item['sha256'] for item in case['inputs']}
    return {'name': 'qasper_fractional_recall_v1',
            'tasks_path': 'data/qasper/tasks.json', 'tasks_sha256': inputs['data/qasper/tasks.json'],
            'corpus_path': 'data/qasper/corpus.json', 'corpus_sha256': inputs['data/qasper/corpus.json']}


def independent_result_audit(out, dataset, required=False):
    """Re-score saved host artifacts without importing generated experiment code.

    A project script can mislabel a metric (for example, call Hit@5 Recall@5).
    The host audit is therefore a separate acceptance gate for campaigns that
    claim a fixed metric contract. Raw measurements remain untouched; the
    audit records both reported and independently recomputed values.
    """
    if not dataset:
        return {'required': bool(required), 'completed': False, 'scores_reproduced': False,
                'reason': 'no audit dataset configured'}
    folder = out / 'independent-audit'
    try:
        from evals.audit_auto_research_results import audit
        result = audit(out, folder, dataset)
        lines = [f'# Independent {dataset} result audit', '',
                 'Saved result artifacts were rescored by the host evaluator. '
                 'Generated experiment code was not imported.', '',
                 f"- scores_reproduced: **{result['scores_reproduced']}**",
                 f"- measurements: **{len(result['measurements'])}**", '']
        names = ('answer_f1', 'exact_match', 'evidence_recall_at_5') if dataset == 'locomo' else ('recall_at_5', 'mrr_at_5')
        lines += ['| task | name | role | ' + ' | '.join(f'reported {name} | independent {name}' for name in names) + ' |',
                  '|---|---|---|' + '---:|---:|' * len(names)]
        for row in result['measurements']:
            reported, independent = row['reported_metrics'], row['metrics']
            values = ' | '.join(f"{reported.get(name, float('nan')):.6f} | {independent.get(name, float('nan')):.6f}" for name in names)
            lines.append(f"| {row['task_id'][:12]} | {row['name']} | {row['role']} | {values} |")
        lines += ['', 'The audit does not establish scientific novelty or held-out validity.']
        (folder / 'report.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
        return {'required': bool(required), 'completed': True,
                'scores_reproduced': result['scores_reproduced'], 'measurements': len(result['measurements']),
                'path': 'independent-audit', 'scope': result['scope']}
    except Exception as exc:
        folder.mkdir(parents=True, exist_ok=True)
        error = str(redact(str(exc)))[:2000]
        (folder / 'report.md').write_text('# Independent result audit\n\nAudit failed: ' + error + '\n', encoding='utf-8')
        return {'required': bool(required), 'completed': False, 'scores_reproduced': False,
                'path': 'independent-audit', 'error': error}


def campaign(out, data, model_id, initial_source=None, prepare_workspace=None):
    from main import build_search
    from research_agent import HttpReader, ResearchAgent
    from research_agent.usage import summarize_usage
    load_dotenv(ROOT/'.env')
    models = []
    def factory(**kwargs):
        folder = out/'models'/f'{len(models):03d}'
        folder.mkdir(parents=True)
        model = instrument(model_from_env(model_id), folder,
                           MODEL_LIMITS)
        models.append(model)
        return model
    def agent(observer, **kwargs):
        return ResearchAgent(build_search(), factory(), out/'traces', reader=HttpReader(), on_event=observer)
    store = WorkbenchStore(out/'state.sqlite')
    app = Workbench(store, factory, agent, out/'traces', start_worker=False)
    case = data['campaign']
    try:
        space = store.save_space({'name': case['id'], 'download_root': 'D:\\paper'})['id']
        chat = store.create_conversation(space, case['id'])['id']
        goal = case['goal']
        if initial_source:
            goal += '\n项目已放入此前保存的实验代码；历史输出不计作本次测量。先检查入口与数据合同，能执行时保存方案并优先用run_experiments取得本次宿主结果；仅在发现具体实现错误或需要方法修订时调用coding_agent。随后按研究目标完成反馈分析、方法修订与重跑。'
        job = app.auto_research.enqueue(space, chat, goal, model_id=model_id,
                                        budget=case['budget'], targets=case.get('targets'),
                                        metric_contract=campaign_metric_contract(case))
        workspace = app.coding.work_root/job['id']
        initial = stage_initial_source(workspace, initial_source) if initial_source else None
        staged = stage_campaign_inputs(workspace, case)
        staged['initial_source'] = initial
        if prepare_workspace:
            staged['preparation'] = prepare_workspace(app, job, workspace, case)
        write(out/'inputs.json', staged)
        write(out/'job.json', job)
        app.coding.start()
        app.worker.start()
        started = time.monotonic()
        previous = None
        while True:
            job = store.job(space, job['id'])
            signature = (job['status'], job['stage'], job['recent_action'])
            if signature != previous:
                print(json.dumps({'status': job['status'], 'stage': job['stage'], 'action': job['recent_action']}, ensure_ascii=False), flush=True)
                previous = signature
            write(out/'progress.json', {'job': job, 'elapsed_seconds': round(time.monotonic()-started, 2)})
            if job['status'] in {'completed', 'failed', 'cancelled', 'interrupted'}:
                break
            if time.monotonic()-started > case['wall_seconds']:
                store.cancel(space, job['id'])
            time.sleep(2)
        detail = app.auto_research.detail(space, job['id'])
        write(out/'detail.json', detail)
        state = app.auto_research.state(job)
        write(out/'controller-state.json', state)
        markdown = job['summary'] or job['error']
        for child in detail['research']:
            folder = out/'research'/child['id']
            folder.mkdir(parents=True)
            original_job = store.job(space, child['id'])
            (folder/'report.md').write_text(original_job['summary'] or original_job['error'], encoding='utf-8')
            (folder/'report.html').write_text(render_report(original_job['summary'] or original_job['error'], original_job['question']), encoding='utf-8')
            if child['run_id']:
                write(folder/'run.json', store.get(child['run_id']))
            markdown = markdown.replace(child['report_url'], 'research/'+child['id']+'/report.html')
        (out/'report.md').write_text(markdown, encoding='utf-8')
        (out/'report.html').write_text(render_report(markdown, case['id']), encoding='utf-8')
        measured = [m for m in app.auto_research.measurement_summary(job, state)['measurements'] if m['valid']]
        roles = {m['role'] for m in measured}
        model_requests = [read(p) for p in sorted((out/'models').glob('*/requests/*.json'))]
        model_inputs = [r.get('messages', []) for r in model_requests]
        tasks = [app.coding.get(space, t['id']) for t in detail['coding']]
        iteration = method_iteration_evidence(model_requests, tasks, detail['plans'])
        write(out/'method-iteration.json', iteration)
        write(out/'coding-tasks.json', tasks)
        independent_audit = independent_result_audit(
            out, case.get('audit_dataset'), required=case.get('requires_independent_audit', False))
        assessment = assessment_evidence(model_requests, tasks, state.get('assessments', []), case.get('targets') or [])
        write(out/'result-assessment.json', assessment)
        feedback = feedback_received(model_inputs, detail['coding'])
        seed_check = multi_seed_evidence(measured, case.get('seeds')) if case.get('requires_multi_seed') else {'required': [], 'passed': True, 'rows': []}
        metrics = {'job_status': job['status'], 'outcome': detail['outcome'], 'research_jobs': len(detail['research']),
                   'plan_versions': len(detail['plans']), 'coding_calls': sum(t['operation'] == 'coding_agent' for t in detail['coding']),
                   'execution_only_calls': sum(t['operation'] == 'run_experiments' for t in detail['coding']), 'valid_measurement_roles': sorted(roles),
                   **feedback, 'cli_executions': sum((out/'coding-tools'/t['id']/'codex.json').is_file() for t in detail['coding']),
                   'usage': summarize_usage([r for m in models for r in m.usage_records]),
                   'multi_seed_evidence': seed_check,
                   'independent_result_audit': independent_audit,
                   'scientific_effect_verified': False, 'seconds': time.monotonic()-started}
        metrics['functional_path_completed'] = bool(job['status'] == 'completed' and detail['plans']
            and detail['outcome'] in {'reported', 'goal_met'} and any(r.get('evidence') for r in detail['research'])
            and {'baseline', 'candidate', 'ablation'} <= roles and feedback['model_received_experiment_results']
            and seed_check['passed']
            and any(all(any(m['role'] == role and app.auto_research.comparable(m, baseline) for m in measured)
                        for role in ('candidate', 'ablation')) for baseline in measured if baseline['role'] == 'baseline'))
        metrics['feedback_iteration_path_completed'] = metrics['functional_path_completed'] and iteration['structural_path_completed']
        metrics['requires_method_iteration'] = bool(case.get('requires_method_iteration'))
        metrics['passed'] = metrics['feedback_iteration_path_completed'] if metrics['requires_method_iteration'] else metrics['functional_path_completed']
        metrics['requires_result_assessment'] = bool(case.get('requires_result_assessment'))
        metrics['result_assessment_path_completed'] = assessment['passed']
        if metrics['requires_result_assessment']:
            metrics['passed'] = metrics['passed'] and assessment['passed']
        if case.get('requires_independent_audit'):
            metrics['passed'] = metrics['passed'] and independent_audit['completed'] and independent_audit['scores_reproduced']
        write(out/'metrics.json', metrics)
        return metrics['passed']
    finally:
        app.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage', choices=['native', 'routing', 'reading', 'recover-reading', 'campaign', 'iterate'], required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--model', default='sudocode-luna')
    parser.add_argument('--case-file', type=Path, help='Use an explicitly versioned acceptance case file.')
    parser.add_argument('--resume-from', type=Path)
    parser.add_argument('--request-timeout', type=int)
    parser.add_argument('--initial-source', type=Path, help='Seed campaign/iterate with a saved source.json snapshot; all experiments run anew')
    args = parser.parse_args()
    if args.initial_source is not None and (args.stage not in {'campaign', 'iterate'} or not args.initial_source.is_file()):
        parser.error('--initial-source requires campaign/iterate and an existing source snapshot file')
    if (args.stage == 'recover-reading') != bool(args.resume_from):
        parser.error('--resume-from is required only for recover-reading')
    if args.request_timeout is not None and (args.stage != 'recover-reading' or not 10 <= args.request_timeout <= 300):
        parser.error('--request-timeout requires recover-reading and must be 10..300 seconds')
    out = args.output.resolve()
    if out.exists():
        raise ValueError('Preserve prior attempts; select a new output directory')
    out.mkdir(parents=True)
    input_data = (args.case_file.resolve() if args.case_file else
                  READING_DATA if args.stage in {'reading', 'recover-reading'} else
                  ITERATION_DATA if args.stage == 'iterate' else DATA)
    if not input_data.is_file():
        parser.error(f'case file not found: {input_data}')
    data = read(input_data)
    write(out/'cases.json', data)
    source_paths = [*ROOT.glob('research_agent/*.py'), *ROOT.glob('evals/*.py')]
    write(out/'condition.json', {'stage': args.stage, 'initial_source': str(args.initial_source.resolve()) if args.initial_source else None, 'request_timeout_override': args.request_timeout, 'started_at': now_iso(), 'data_path': str(input_data.relative_to(ROOT)), 'data_sha256': hashlib.sha256(input_data.read_bytes()).hexdigest(),
          'source_sha256': {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in source_paths},
          'scientific_effect_measurement': False, 'campaign_model_limits': MODEL_LIMITS})
    with zipfile.ZipFile(out/'evaluated-source-and-cases.zip', 'x', compression=zipfile.ZIP_DEFLATED) as archive:
        for path in [*source_paths, input_data]:
            archive.write(path, str(path.relative_to(ROOT)))
        if args.initial_source:
            archive.write(args.initial_source, 'initial-source.json')
    passed = native(out, data) if args.stage == 'native' else routing(out, data, args.model) if args.stage == 'routing' else reading(out, data, args.model) if args.stage == 'reading' else recover_reading(out, data, args.model, args.resume_from, args.request_timeout) if args.stage == 'recover-reading' else campaign(out, data, args.model, args.initial_source)
    print(json.dumps({'passed': passed, 'output': str(out)}), flush=True)
    return 0 if passed else 1


if __name__ == '__main__':
    raise SystemExit(main())
