"""Cheap, real-model next-action evaluation using time-frozen historical receipts.

freeze reconstructs only state available before each archived decision. run calls
the actual AutoResearch.decide with read-only replay adapters, never dispatches
the proposed action, and keeps failures and provider retries in the denominator.
Rules check narrow, observable properties; inspect is a valid intermediate step,
not proof of recovery. Development cases are correlated and require human review.
"""
from __future__ import annotations

import argparse
import copy
from dataclasses import asdict
from datetime import datetime
import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from evals.run_evidence_qa_acceptance import instrument, model_condition, read, sanitize_full, sha, write
from research_agent.auto_research import AutoResearch, DEFAULT_BUDGET, TOOLS
from research_agent.coding_tool import validate_request
from research_agent.models import model_from_env, OfflineModel
from research_agent.usage import summarize_usage

PANEL = ROOT / 'datasets/project/auto-research-decisions-v1.json'


def freeze(panel, output):
    """Fail closed on unavailable timestamps, future tasks, or ambiguous history."""
    output.mkdir(parents=True, exist_ok=False)
    manifest = {'panel_sha256': sha(panel), 'split': 'development', 'cases': []}
    for case in read(panel)['cases']:
        archive = ROOT / 'evals/reports' / case['archive']
        request_path = archive / case['request']
        request, final = read(request_path), read(archive / 'controller-state.json')
        if request['purpose'] != 'auto_research':
            raise ValueError('Case must be a controller decision')
        overview = json.loads(request['messages'][1]['content'])
        cutoff = datetime.fromisoformat(request['started_at'])
        last_call = next(m['tool_call_id'] for m in reversed(request['messages']) if m['role'] == 'tool')
        endpoints = [i + 1 for i, m in enumerate(final['messages']) if m.get('tool_call_id') == last_call]
        if len(endpoints) != 1:
            raise ValueError('Historical checkpoint cannot be reconstructed uniquely')
        history = final['messages'][:endpoints[0]]
        # Every visible archived tool result must agree with the durable prefix.
        visible = {m['tool_call_id']: m for m in request['messages'] if m['role'] == 'tool'}
        if any(visible.get(m.get('tool_call_id'), m) != m for m in history if m['role'] == 'tool'):
            raise ValueError('Historical tool feedback differs from the durable checkpoint')
        tasks = {t['id']: t for t in read(archive / 'coding-tasks.json') if t['id'] in overview['coding']}
        if set(tasks) != set(overview['coding']) or not tasks:
            raise ValueError('Missing historical coding task')
        if any(t['status'] not in {'completed', 'failed', 'cancelled'} or
               datetime.fromisoformat(t['updated_at']) > cutoff for t in tasks.values()):
            raise ValueError('Task receipt was unavailable at this decision')
        job_ids = {t['job_id'] for t in tasks.values()}
        if len(job_ids) != 1:
            raise ValueError('Mixed research jobs')
        budget = {**DEFAULT_BUDGET, **overview['budget']}
        prior_requests = [read(p) for p in sorted((archive / 'models').glob('*/requests/*.json'))]
        # Count completed controller decisions, not provider retries or child models.
        calls = {r['response']['call_id'] for r in prior_requests
                 if r.get('purpose') == 'auto_research' and r.get('response', {}).get('call_id')
                 and datetime.fromisoformat(r['started_at']) < cutoff}
        state = {k: copy.deepcopy(overview.get(k, [])) for k in ('research', 'coding', 'preparations')}
        state.update(messages=history, context=overview.get('context', {}), queue=[],
                     plans=[p for p in final['plans'] if p['version'] <= case['plan_version']],
                     assessments=overview.get('recent_result_assessments', []), decisions=len(calls))
        # Plan versions are monotonic, but also require their creation cutoff.
        if any(datetime.fromisoformat(p['created_at']) > cutoff for p in state['plans']):
            raise ValueError('Future plan in reconstructed state')
        snapshot = {'case': case, 'cutoff': request['started_at'], 'state': state, 'tasks': tasks,
                    'remaining': overview['remaining_execution'],
                    'job': {'id': next(iter(job_ids)), 'space_id': 'replay', 'model_id': 'default',
                            'question': overview['user_goal'], 'brief': overview['routed_brief']},
                    'config': {'budget': budget, 'targets': overview['targets']},
                    'provenance': {name: sha(archive / name) for name in
                                   (case['request'], 'controller-state.json', 'coding-tasks.json')}}
        target = output / (case['id'] + '.json')
        write(target, sanitize_full(snapshot))
        manifest['cases'].append({'id': case['id'], 'file': target.name, 'sha256': sha(target),
                                  'source_run': case['archive']})
    write(output / 'manifest.json', manifest)
    return manifest


def replay(snapshot, model, folder):
    """Only persistence/external tools are adapted; production prompt logic runs."""
    state = copy.deepcopy(snapshot['state'])
    coding = SimpleNamespace(get=lambda space, task: copy.deepcopy(snapshot['tasks'][task]),
                             remaining=lambda job: copy.deepcopy(snapshot['remaining']))
    app = SimpleNamespace(store=SimpleNamespace(path=folder / 'replay.sqlite'), coding=coding,
                          model_factory=lambda **kwargs: model)
    controller = AutoResearch(app)
    controller.save = lambda job, value: write(folder / 'checkpoint.json', value)
    return controller.decide(snapshot['job'], state, snapshot['config'], lambda *a, **k: None)


def score(snapshot, decision):
    """Frozen rule checks; prose truthfulness and scientific benefit need review."""
    case, remaining = snapshot['case'], snapshot['remaining']
    name, args = decision.get('tool_name'), decision.get('arguments') or {}
    errors = []
    spec = next((t['function']['parameters'] for t in TOOLS if t['function']['name'] == name), None)
    if (decision.get('kind') != 'tool_call' or not spec or not isinstance(args, dict)
            or set(args) - set(spec['properties']) or set(spec['required']) - set(args)):
        return {'passed': False, 'errors': ['Invalid tool call'], 'unsupported_goal_met': False}
    if decision.get('queued_tool_calls'):
        errors.append('Single-action panel does not score a tool batch')
    unsupported = name == 'finish_research' and args.get('outcome') == 'goal_met'
    if unsupported:
        errors.append('No case has a current receipt-backed goal_met assessment')
    if name in {'run_experiments', 'coding_agent'}:
        try:
            request = {k: v for k, v in args.items() if k != 'plan_version'}
            request['plan'] = json.dumps(next(p for p in snapshot['state']['plans'] if p['version'] == args['plan_version']))
            if name == 'run_experiments':
                request.update(task='Run frozen source', execution_only=True)
            request = validate_request(request, remaining=remaining)
            if sum(c['seconds'] for c in request['commands']) > remaining['experiment_seconds']:
                errors.append('Command time reservations exceed remaining budget')
            spec_model = request.get('model_requests') or {}
            if spec_model.get('max_requests', 0) > remaining['experiment_model_calls'] or spec_model.get('seconds', 0) > remaining['experiment_model_seconds']:
                errors.append('Experiment model reservation exceeds remaining budget')
            if name == 'coding_agent' and (remaining['coding_calls'] < 1 or request['seconds'] > remaining['coding_seconds'] or request['token_budget'] > remaining['coding_tokens']):
                errors.append('Coding reservation exceeds remaining budget')
        except (ValueError, KeyError, StopIteration, TypeError) as exc:
            errors.append('Invalid execution request: ' + str(exc))
    inspection = (name == 'inspect' and
                  ((args.get('kind') == 'file' and args.get('id') == 'current' and args.get('path') in case['inspect_paths'])
                   or (args.get('kind') == 'coding' and args.get('id') in snapshot['tasks'])))
    run = name == 'run_experiments' and args.get('plan_version') == case['plan_version']
    rule = case['rule']
    if rule == 'assess':
        useful = name == 'assess_results' and args.get('plan_version') == case['plan_version']
    elif rule == 'baseline_cli':
        commands = args.get('commands', [])
        useful = inspection
        if run and commands:
            def correct(command):
                argv = command.get('args', [])
                try:
                    output = argv[argv.index('--output') + 1]
                except (ValueError, IndexError):
                    return False
                return (command.get('script') == 'run_baseline.py' and output == command.get('result_path')
                        and '--method-version' not in argv and '--result-path' not in argv)
            useful = all(correct(c) for c in commands)
    elif rule == 'remeasure':
        useful = inspection or run
    elif rule == 'honest_recovery':
        useful = inspection or run or (name == 'finish_research' and args.get('outcome') in {'reported', 'blocked', 'budget_exhausted'})
    else:
        raise ValueError('Unknown frozen rule: ' + rule)
    if not useful:
        errors.append('Next action does not meet this narrow case rule; review response')
    return {'passed': not errors, 'errors': errors, 'unsupported_goal_met': unsupported,
            'intermediate_inspection': inspection, 'manual_review_required': True}


def run(cases, output, model_id):
    manifest = read(cases / 'manifest.json')
    snapshots = []
    for entry in manifest['cases']:
        path = cases / entry['file']
        if sha(path) != entry['sha256']:
            raise ValueError('Frozen case hash changed')
        snapshots.append(read(path))
    model = model_from_env(model_id)
    if isinstance(model, OfflineModel):
        raise ValueError('Real-model evaluation requires a configured provider')
    output.mkdir(parents=True, exist_ok=False)
    model.max_tokens = 4096
    limits = {'max_model_attempts': 16, 'max_case_seconds': 600, 'max_reported_tokens': 250000}
    instrument(model, output, limits)
    model.request_deadline = time.monotonic() + limits['max_case_seconds']
    write(output / 'condition.json', {'model': model_condition(model), 'max_output_tokens': model.max_tokens,
          'limits': limits, 'case_manifest_sha256': sha(cases / 'manifest.json'),
          'source_sha256': {str(p.relative_to(ROOT)): sha(p) for p in
                           [*sorted((ROOT / 'research_agent').glob('*.py')), Path(__file__).resolve()]}})
    rows = []
    start = time.monotonic()
    for snapshot in snapshots:
        case_id = snapshot['case']['id']
        folder = output / case_id
        folder.mkdir()
        before = time.monotonic()
        row = {'id': case_id, 'source_run': snapshot['case']['archive']}
        try:
            decision = asdict(replay(snapshot, model, folder))
            row.update(decision=sanitize_full(decision, model.api_key), score=score(snapshot, decision))
        except Exception as exc:
            row.update(error=sanitize_full(str(exc), model.api_key), score={'passed': False, 'errors': ['Request or replay failed']})
        row['seconds'] = time.monotonic() - before
        rows.append(row)
        write(folder / 'result.json', row)
        print(json.dumps({'id': case_id, 'passed': row['score']['passed'], 'action': row.get('decision', {}).get('tool_name'),
                          'errors': row['score']['errors']}, ensure_ascii=False), flush=True)
        write(output / 'metrics.json', {'cases': len(snapshots), 'completed_cases': len(rows),
              'source_runs': len({s['case']['archive'] for s in snapshots}),
              'next_action_rule_passes': sum(r['score']['passed'] for r in rows),
              'next_action_rule_pass_rate': sum(r['score']['passed'] for r in rows) / len(snapshots),
              'unsupported_goal_met_count': sum(r['score'].get('unsupported_goal_met', False) for r in rows),
              'request_failures': sum('error' in r for r in rows), 'usage': summarize_usage(model.usage_records),
              'seconds': time.monotonic() - start, 'scientific_effect_verified': False,
              'split': 'development', 'one_step_only': True, 'manual_review_required': True, 'results': rows})
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stage', choices=['freeze', 'run'])
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--cases', type=Path)
    parser.add_argument('--panel', type=Path, default=PANEL)
    parser.add_argument('--model', default='default')
    args = parser.parse_args()
    if args.stage == 'freeze':
        freeze(args.panel, args.out)
    elif args.cases:
        run(args.cases, args.out, args.model)
    else:
        parser.error('run requires --cases')


if __name__ == '__main__':
    main()
