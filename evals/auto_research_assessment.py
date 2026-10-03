"""Audit actual controller decisions against preserved host results and model requests."""
from __future__ import annotations

import json
from datetime import datetime
import math

from research_agent.auto_research import AutoResearch


def measurements_before(requests, tasks, request_index):
    """Host results available by this decision, not measurements produced later."""
    by_id = {task['id']: task for task in tasks}
    rows = {}
    cutoff = requests[request_index].get('started_at')
    for task in tasks:
        if task.get('status') not in {'completed', 'failed', 'cancelled'}:
            continue
        try:
            ready = datetime.fromisoformat(task['updated_at']) <= datetime.fromisoformat(cutoff)
        except (KeyError, TypeError, ValueError):
            ready = False
        if ready:
            for m in AutoResearch.measurements_for_comparison(task):
                if m.get('valid') is True:
                    rows[task['id'], m['measurement_index']] = m
    # Older receipts may lack timestamps; a matching visible result proves availability.
    for request in requests[:request_index + 1]:
        if request.get('purpose') != 'auto_research':
            continue
        for message in request.get('messages', []):
            if message.get('role') != 'tool':
                continue
            try:
                value = json.loads(message['content'])['UNTRUSTED_TOOL_DATA']
            except (KeyError, TypeError, ValueError):
                continue
            task = by_id.get(value.get('id')) if isinstance(value, dict) else None
            if not task:
                continue
            terminal = value.get('status') in {'completed', 'failed', 'cancelled'} and value['status'] == task.get('status')
            for m in AutoResearch.measurements_for_comparison(task):
                if m.get('valid') is True and (terminal or any(
                        isinstance(f, dict) and f.get('valid') is True and f.get('name') == m['name']
                        and f.get('metrics') == m['metrics'] for f in value.get('measurements', []))):
                    rows[task['id'], m['measurement_index']] = m
    return list(rows.values())


def assessment_evidence(requests, tasks, assessments, targets):
    by_id = {t['id']: t for t in tasks}
    calls, received = {}, {}
    for index, request in enumerate(requests):
        if request.get('purpose') != 'auto_research':
            continue
        response = request.get('response') or {}
        decisions = [response, *response.get('queued_tool_calls', [])]
        for decision in decisions:
            if decision.get('tool_name') == 'assess_results':
                calls[decision.get('call_id')] = (index, decision)
        for message in request.get('messages', []):
            if message.get('role') != 'tool':
                continue
            try:
                value = json.loads(message['content'])['UNTRUSTED_TOOL_DATA']
            except (KeyError, TypeError, ValueError):
                continue
            if isinstance(value, dict):
                received.setdefault(message.get('tool_call_id'), []).append((index, value, response))
    rows = []
    for assessment in assessments:
        call_id = assessment.get('call_id')
        version = assessment.get('plan_version')
        call = calls.get(call_id)
        checks, seen_tasks = [], set()
        pairs = assessment.get('comparable_pairs') or []
        valid = bool(call and call[1].get('arguments', {}).get('plan_version') == version)
        available = {}
        host_rows = [m for m in measurements_before(requests, tasks, call[0]) if m['plan_version'] <= version] if call else []
        host_available = {(m['task_id'], m['measurement_index']): (by_id[m['task_id']], m) for m in host_rows}
        for entries in received.values():
            for i, value, _ in entries:
                task = by_id.get(value.get('id'))
                if not call or i > call[0] or not task or json.loads(task['request']['plan'])['version'] > version:
                    continue
                terminal_feedback = value.get('status') in {'completed', 'failed', 'cancelled'} \
                    and value.get('status') == task.get('status')
                for m in AutoResearch.measurements_for_comparison(task):
                    # Terminal receipts cannot gain more measurements. Their full
                    # count survives model-facing truncation, while paired-result
                    # consumption below still requires visible candidate metrics.
                    if terminal_feedback and m.get('valid') is True:
                        host_available[task['id'], m['measurement_index']] = (task, m)
                    if m.get('valid') is True and any(
                            isinstance(f, dict) and f.get('valid') is True and f.get('name') == m['name']
                            and f.get('metrics') == m['metrics'] for f in value.get('measurements', [])):
                        available[task['id'], m['name']] = (task, m)
                        host_available[task['id'], m['measurement_index']] = (task, m)
        for pair in pairs:
            matched = []
            for role, key in [('candidate', 'task_id'), ('baseline', 'baseline_task_id')]:
                task = by_id.get(pair.get(key))
                task_version = json.loads(task['request']['plan'])['version'] if task else None
                if not task or (task_version != version if role == 'candidate' else task_version > version):
                    valid = False
                    continue
                values = [m for m in AutoResearch.measurements_for_comparison(task) if m.get('valid') is True
                          and m.get('name') == pair.get(role) and m.get('role') == role
                          and m.get('metrics') == pair.get(role + '_metrics')]
                if len(values) != 1 or (task['id'], values[0]['measurement_index']) not in host_available:
                    valid = False
                    continue
                matched.append(values[0])
                seen_tasks.add(task['id'])
            if len(matched) != 2 or not AutoResearch.comparable(*matched):
                valid = False
                continue
            candidates_for_baseline = [m for _, m in host_available.values()]
            selected = AutoResearch.baseline_matches(matched[0], candidates_for_baseline)
            if (len(selected) != 1 or selected[0]['task_id'] != matched[1]['task_id']
                    or selected[0]['measurement_index'] != matched[1]['measurement_index']):
                valid = False
                continue
            pair_checks = []
            for target in targets:
                value = matched[0]['metrics'].get(target['name'])
                before = matched[1]['metrics'].get(target['name'])
                numeric = type(value) in (int, float) and math.isfinite(value)
                sign = 1 if target['direction'] == 'higher' else -1
                pair_checks.append((numeric and sign * value >= sign * target['value'],
                                    numeric and type(before) in (int, float) and math.isfinite(before)
                                    and sign * value > sign * before))
            checks.append(pair_checks)
        expected = ('review_required' if not targets else 'goal_met'
                    if any(all(met for met, _ in group) for group in checks) else 'revise_method'
                    if any(improved for group in checks for _, improved in group) else 'continue_research')
        if not pairs:
            candidates = [(t, m) for t, m in host_available.values() if m.get('role') == 'candidate' and m['plan_version'] == version]
            host_rows = [m for _, m in host_available.values()]
            has_pair = any(len(AutoResearch.baseline_matches(candidate, host_rows)) == 1 for _, candidate in candidates)
            expected = 'blocked' if candidates else 'collect_measurements'
            valid = valid and not has_pair and type(assessment.get('measurement_count')) is int \
                and assessment['measurement_count'] == len(host_available)
        # A retained assessment alone does not prove the LLM requested or consumed it.
        following = [(i, response) for i, value, response in received.get(call_id, [])
                     if call and i > call[0] and value.get('recommendation') == expected
                     and value.get('plan_version') == version and response.get('kind') == 'tool_call']
        seen_results = {task['id'] for task, m in available.values() if m.get('role') == 'candidate'}
        valid = bool(valid and assessment.get('recommendation') == expected and following
                     and (not pairs or seen_results.intersection(pair.get('task_id') for pair in pairs)))
        rows.append({'call_id': call_id, 'plan_version': version, 'valid': valid,
                     'expected_recommendation': expected, 'recorded_recommendation': assessment.get('recommendation'),
                     'has_comparable_pairs': bool(pairs), 'source_tasks': sorted(seen_tasks), 'following_tool': following[0][1].get('tool_name') if following else None})
    required = {json.loads(t['request']['plan'])['version'] for t in tasks
                if any(m.get('valid') is True and m.get('role') == 'candidate' for m in t['state'].get('measurements', []))}
    covered = {r['plan_version'] for r in rows if r['valid'] and r['has_comparable_pairs']}
    return {'passed': bool(required) and required <= covered and all(r['valid'] for r in rows),
            'required_plan_versions': sorted(required), 'assessed_plan_versions': sorted(covered), 'rows': rows,
            'scope': 'Controller requested and consumed a receipt-backed assessment per measured plan; not scientific validity.'}
