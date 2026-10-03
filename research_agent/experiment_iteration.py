"""A bounded outer loop around the existing receipt-bound development turn."""
from __future__ import annotations

import copy
import difflib
import inspect
import json
import math
from pathlib import Path

from . import experiment_feedback as feedback
from .experiment_retrieval import dump, sha
from .trace import now_iso, redact
from .workbench_store import Conflict


ROUND_NAMES = ('one', 'two', 'three')
ROUND_FILES = {'codex.json', 'events.jsonl', 'ranker.py', 'submitted-ranker.py',
               'proposal.md', 'development-snapshots.json', 'diff.patch', 'dev-feedback.json', 'failure.json'}


def policy_hash(executor):
    return sha(Path(__file__).read_bytes() + inspect.getsource(executor._develop_round).encode())


def artifact_name(name):
    for index, word in enumerate(ROUND_NAMES, 1):
        prefix = 'round-' + word + '-'
        if name.startswith(prefix) and name[len(prefix):] in ROUND_FILES:
            return f'round-{index:02d}/' + name[len(prefix):]
    return None


def accounting(path, invoked):
    """Unknown consumption closes the budget; it never becomes zero cost."""
    if not invoked:
        return {'seconds': 0, 'tokens': 0, 'complete': True, 'usage': []}
    try:
        receipt = json.loads((path / 'codex.json').read_text(encoding='utf-8'))
        events = [json.loads(line) for line in receipt['stdout'].splitlines() if line.startswith('{')]
        terminals = [event for event in events if event.get('type') in ('turn.completed', 'turn.failed')]
        usage = [event.get('usage', {}) for event in terminals]
        seconds = receipt.get('seconds')
        known_time = type(seconds) in (int, float) and math.isfinite(seconds) and seconds >= 0
        known_tokens = bool(usage) and all(type(u.get(key)) is int and u[key] >= 0
                                         for u in usage for key in ('input_tokens', 'output_tokens'))
        return {'seconds': seconds if known_time else None,
                'tokens': sum(u['input_tokens'] + u['output_tokens'] for u in usage) if known_tokens else None,
                'complete': bool(known_time and known_tokens), 'usage': usage,
                'termination': receipt.get('termination')}
    except (OSError, ValueError, KeyError, TypeError):
        return {'seconds': None, 'tokens': None, 'complete': False, 'usage': []}


def totals(rounds):
    used = [row['accounting'] for row in rounds if row.get('coding_invocations')]
    return {'coding_invocations': len(used),
            'seconds': sum(row['seconds'] for row in used if row['seconds'] is not None),
            'tokens': sum(row['tokens'] for row in used if row['tokens'] is not None),
            'complete': all(row['complete'] for row in used),
            'boundary': 'Coding process seconds and reported input + output tokens, including cached input; evidence preparation is accounted separately.'}


def target_met(baseline, best, metrics):
    for metric in metrics:
        name = metric['name']
        before, value = baseline.get(name), best.get(name)
        if type(before) not in (int, float) or type(value) not in (int, float): return False
        improvement = (value - before) * (1 if metric['direction'] == 'higher' else -1)
        mode = metric['target_mode']
        if mode == 'absolute':
            met = value >= metric['target'] if metric['direction'] == 'higher' else value <= metric['target']
        elif mode == 'delta': met = improvement >= metric['target']
        else: met = bool(before) and improvement / abs(before) * 100 >= metric['target']
        if not met: return False
    return True


def run(executor, job, contract, state, suite, workspace, output, data, private, cancelled, stage, tree):
    """Reserve every coding turn before launch; resume only completed host receipts."""
    budget = contract['budget']
    loop = state.setdefault('iteration', {
        'schema_version': 'bounded-development/v1', 'max_iterations': budget['coding_invocations'],
        'rounds': [], 'consecutive_no_improvement': 0, 'stop_reason': None,
        'best': {**state['development']['baseline'], 'code': suite.BASELINE,
                 'measurement': state['baseline']},
    })

    def checkpoint():
        loop['totals'] = totals(loop['rounds'])
        state['usage'] = [usage for row in loop['rounds'] for usage in row.get('accounting', {}).get('usage', [])]
        dump(output / 'iterations.json', loop)
        executor.save(job, state)

    def freeze():
        best = loop['best']
        current = tree()
        mutable = {'ranker.py', 'proposal.md', 'candidate-first.json', 'candidate-revised.json'}
        if set(current) != set(state['prepared']) or any(current[key] != value
                for key, value in state['prepared'].items() if key not in mutable):
            raise Conflict('开发资料或评测工具发生变化，拒绝冻结最佳候选')
        # Rebuild only from host-pinned source, never the last submitted candidate.
        if sha(best['code']) != best['code_hash']: raise Conflict('最佳版本检查点哈希不一致')
        (workspace / 'ranker.py').write_text(best['code'], encoding='utf-8')
        (output / 'ranker.py').write_text(best['code'], encoding='utf-8')
        (output / 'diff.patch').write_text(''.join(difflib.unified_diff(
            suite.BASELINE.splitlines(True), best['code'].splitlines(True),
            fromfile='baseline/ranker.py', tofile='selected/ranker.py')), encoding='utf-8')
        state.update(code_hash=best['code_hash'], selected_label=best['label'], development_selected=True)
        loop['frozen_code_hash'] = best['code_hash']
        if best['label'].startswith('round-'):
            selected_round = int(best['label'].split('-')[1])
            proposal = output / f'round-{selected_round:02d}' / 'proposal.md'
            if proposal.is_file(): (output / 'proposal.md').write_bytes(proposal.read_bytes())
        dump(output / 'development-snapshots.json', state['development'])
        checkpoint()

    while not loop['stop_reason']:
        if cancelled(): raise Conflict('执行已停止')
        active = loop['rounds'][-1] if loop['rounds'] and loop['rounds'][-1]['status'] != 'completed' else None
        if active is None:
            spent = totals(loop['rounds'])
            remaining_seconds = budget['coding_seconds'] - spent['seconds']
            remaining_tokens = budget['token_budget'] - spent['tokens']
            if not spent['complete']: loop['stop_reason'] = 'usage_incomplete'
            elif len(loop['rounds']) >= loop['max_iterations']: loop['stop_reason'] = 'max_iterations'
            elif remaining_seconds < 1 or remaining_tokens < 2000: loop['stop_reason'] = 'budget_exhausted'
            if loop['stop_reason']: break
            index = len(loop['rounds']) + 1
            active = {'index': index, 'status': 'preparing', 'coding_invocations': 0,
                      'started_at': now_iso(), 'before': copy.deepcopy(loop['best']['metrics']),
                      'allowance': {'coding_seconds': remaining_seconds, 'token_budget': remaining_tokens},
                      'accounting': accounting(output, False),
                      'artifacts': {name: 'round-' + ROUND_NAMES[index - 1] + '-' + name for name in sorted(ROUND_FILES)}}
            loop['rounds'].append(active)
            checkpoint()
        round_output = output / f"round-{active['index']:02d}"
        round_output.mkdir(exist_ok=True)
        if active['status'] == 'preparing':
            current = tree()
            # These two dev files may be midway through a host update after a crash.
            mutable = {'ranker.py', 'proposal.md', 'candidate-first.json', 'candidate-revised.json',
                       'dev-feedback.json', 'dev-inputs.json'}
            if set(current) != set(state['prepared']) or any(current[key] != value
                    for key, value in state['prepared'].items() if key not in mutable):
                raise Conflict('轮次之间冻结资料发生变化，拒绝重新固定被修改的语料')
            # Host updates development feedback between turns. Test rows never enter it.
            (workspace / 'ranker.py').write_text(loop['best']['code'], encoding='utf-8')
            for name in ('candidate-first.json', 'candidate-revised.json'): dump(workspace / name, {})
            for name, value in feedback.development_files(data, loop['best']['measurement']).items():
                dump(workspace / name, value)
                dump(round_output / name, value)
            for key in ('code_hash', 'development_selected', 'selected_label'): state.pop(key, None)
            for key in ('snapshots', 'slot_hashes'): state['development'].pop(key, None)
            state['prepared'] = tree()
            active['status'] = 'ready'
            checkpoint()
        if active['coding_invocations'] and not state.get('code_hash') and not executor.coding_receipt(round_output):
            # A reserved process with no completed receipt is never launched again.
            active.update(status='interrupted', stop_reason='coding_interrupted',
                          accounting=accounting(round_output, True))
            loop['stop_reason'] = 'coding_interrupted'
            break
        current_contract = copy.deepcopy(contract)
        current_contract['budget'].update(active['allowance'])
        current_contract['_iteration_prompt'] = ('\nThis is bounded development round '
            + str(active['index']) + '/' + str(loop['max_iterations'])
            + '. ranker.py is the best host-verified development source so far. dev-feedback.json is its measured development feedback. '
            'Propose a concrete general improvement from those remaining failures; keep useful earlier behavior. '
            'No final-test scores or labels are available. Best development metrics: '
            + json.dumps(loop['best']['metrics'], ensure_ascii=False)
            + '. Previous rounds: ' + json.dumps([
                {key: row.get(key) for key in ('index', 'after', 'improved', 'stop_reason')}
                for row in loop['rounds'][:-1]], ensure_ascii=False))
        try:
            executor._develop_round(job, current_contract, state, suite, workspace, round_output,
                                    data, private, cancelled, stage, tree)
        except Exception as exc:
            active['accounting'] = accounting(round_output, active['coding_invocations'])
            failure = {'error': str(redact(str(exc)))[:2000], 'phase': state.get('phase'), 'at': now_iso()}
            active.setdefault('failures', []).append(failure)
            active.update(status='failed', stop_reason='attempt_interrupted')
            dump(round_output / 'failure.json', failure)
            if cancelled():
                active.update(status='interrupted', stop_reason='cancelled')
                loop['stop_reason'] = 'cancelled'
            if active['coding_invocations']:
                submitted = round_output / 'submitted-ranker.py'
                # Development scoring temporarily installs another snapshot in ranker.py.
                # Never replace the host-pinned final submission with that temporary source.
                if not state.get('code_hash') and not submitted.exists():
                    try:
                        submitted.write_text(executor.code_snapshot(workspace), encoding='utf-8')
                    except (OSError, ValueError): pass
                used = active['accounting']
                if used['tokens'] is not None and used['tokens'] > active['allowance']['token_budget']:
                    active['stop_reason'] = loop['stop_reason'] = 'budget_exceeded'
            dump(output / 'iterations.json', {**loop, 'totals': totals(loop['rounds'])})
            if not cancelled(): checkpoint()
            raise
        active['accounting'] = accounting(round_output, active['coding_invocations'])
        selected = next((trial for trial in state['development']['trials']
                         if trial['label'] == state['selected_label']), None)
        best = selected or {**state['development']['baseline'], 'code': suite.BASELINE,
                            'measurement': state['baseline']}
        improved = tuple(best['metrics'][key] for key in ('recall_at5', 'mrr')) > tuple(
            loop['best']['metrics'][key] for key in ('recall_at5', 'mrr'))
        loop['best'] = copy.deepcopy(best)
        loop['consecutive_no_improvement'] = 0 if improved else loop['consecutive_no_improvement'] + 1
        active.update(status='completed', completed_at=now_iso(), improved=improved,
                      after=copy.deepcopy(best['metrics']), selected_label=best['label'],
                      selected_code_hash=best['code_hash'])
        spent = totals(loop['rounds'])
        if not spent['complete']: reason = 'usage_incomplete'
        elif spent['tokens'] >= budget['token_budget'] or spent['seconds'] >= budget['coding_seconds']: reason = 'budget_exhausted'
        elif target_met(state['development']['baseline']['metrics'], best['metrics'], contract['input']['content']['metrics']): reason = 'development_target_met'
        elif loop['consecutive_no_improvement'] >= 2: reason = 'no_improvement'
        elif active['index'] >= loop['max_iterations']: reason = 'max_iterations'
        else: reason = 'continue'
        active['stop_reason'] = reason
        if reason != 'continue': loop['stop_reason'] = reason
        # Keep old top-level artifact links useful; individual turn originals remain immutable.
        for name in ('codex.json', 'events.jsonl', 'submitted-ranker.py', 'proposal.md'):
            source = round_output / name
            if source.is_file(): (output / name).write_bytes(source.read_bytes())
        checkpoint()
    freeze()
