"""Protect decision-eval labels, temporal boundaries and failure accounting."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from evals import run_auto_research_decisions as evaluator


class DecisionEvaluationTests(unittest.TestCase):
    def snapshot(self, rule='remeasure'):
        return {'case': {'rule': rule, 'plan_version': 1, 'inspect_paths': ['run_baseline.py']},
                'remaining': {'experiment_seconds': 100, 'experiment_model_calls': 2,
                              'experiment_model_seconds': 60, 'coding_calls': 1,
                              'coding_seconds': 60, 'coding_tokens': 2000},
                'state': {'plans': [{'version': 1}]}, 'tasks': {'known-task': {}}}

    def decision(self, output='result.json', seconds=20):
        return {'kind': 'tool_call', 'tool_name': 'run_experiments', 'arguments': {
            'plan_version': 1, 'commands': [{'name': 'baseline', 'role': 'baseline',
                'script': 'run_baseline.py', 'args': ['--output', output],
                'result_path': 'result.json', 'seconds': seconds}]}}

    def test_scores_output_contract_and_total_reservation_not_only_tool_name(self):
        snapshot = self.snapshot('baseline_cli')
        self.assertTrue(evaluator.score(snapshot, self.decision())['passed'])
        self.assertFalse(evaluator.score(snapshot, self.decision('wrong.json'))['passed'])
        decision = self.decision(seconds=60)
        decision['arguments']['commands'].append({**decision['arguments']['commands'][0], 'name': 'second'})
        self.assertFalse(evaluator.score(snapshot, decision)['passed'])
        decision = self.decision()
        decision['arguments']['model_requests'] = {'mode': 'stdio', 'model_id': 'default',
                                                 'max_requests': 3, 'seconds': 30}
        self.assertFalse(evaluator.score(snapshot, decision)['passed'])

    def test_invalid_and_unsupported_answers_do_not_pass(self):
        snapshot = self.snapshot('honest_recovery')
        goal = {'kind': 'tool_call', 'tool_name': 'finish_research', 'arguments': {
            'outcome': 'goal_met', 'summary': 'Success', 'limitations': ''}}
        self.assertTrue(evaluator.score(snapshot, goal)['unsupported_goal_met'])
        self.assertFalse(evaluator.score(snapshot, goal)['passed'])
        self.assertFalse(evaluator.score(snapshot, {'kind': 'final', 'content': 'Success'})['passed'])
        inspection = {'kind': 'tool_call', 'tool_name': 'inspect', 'arguments': {'kind': 'coding', 'id': 'unknown-task'}}
        self.assertFalse(evaluator.score(snapshot, inspection)['passed'])

    def test_freeze_excludes_future_history_and_rejects_future_receipts(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archive = root / 'evals/reports/run'
            archive.mkdir(parents=True)
            case = {'id': 'case', 'archive': 'run', 'request': 'request.json', 'plan_version': 1,
                    'rule': 'remeasure', 'inspect_paths': []}
            history = [{'role': 'assistant', 'content': 'past'},
                       {'role': 'tool', 'tool_call_id': 'known', 'content': '{}'}]
            overview = {'coding': ['task'], 'budget': {}, 'targets': [], 'user_goal': 'goal',
                        'routed_brief': 'goal', 'remaining_execution': {}}
            request = {'purpose': 'auto_research', 'started_at': '2026-01-02T00:00:00+00:00',
                       'messages': [{'role': 'system', 'content': ''},
                                    {'role': 'user', 'content': json.dumps(overview)}, *history]}
            task = {'id': 'task', 'job_id': 'job', 'status': 'failed', 'updated_at': '2026-01-01T00:00:00+00:00'}
            evaluator.write(archive / 'request.json', request)
            evaluator.write(archive / 'controller-state.json', {
                'messages': [*history, {'role': 'user', 'content': 'FUTURE RESULT'}],
                'plans': [{'version': 1, 'created_at': '2026-01-01T00:00:00+00:00'}]})
            evaluator.write(archive / 'coding-tasks.json', [task])
            evaluator.write(root / 'panel.json', {'cases': [case]})
            with patch.object(evaluator, 'ROOT', root):
                evaluator.freeze(root / 'panel.json', root / 'frozen')
                frozen = evaluator.read(root / 'frozen/case.json')
                self.assertEqual(frozen['state']['messages'], history)
                task['updated_at'] = '2026-01-03T00:00:00+00:00'
                evaluator.write(archive / 'coding-tasks.json', [task])
                with self.assertRaisesRegex(ValueError, 'unavailable at this decision'):
                    evaluator.freeze(root / 'panel.json', root / 'future')
            # Tampering is rejected before model creation or any paid call.
            frozen['state']['messages'].append({'role': 'user', 'content': 'changed'})
            evaluator.write(root / 'frozen/case.json', frozen)
            with patch.object(evaluator, 'model_from_env') as model:
                with self.assertRaisesRegex(ValueError, 'hash changed'):
                    evaluator.run(root / 'frozen', root / 'result', 'default')
                model.assert_not_called()


if __name__ == '__main__':
    unittest.main()
