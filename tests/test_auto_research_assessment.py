"""A tool call, saved label or invented metrics alone cannot pass result feedback acceptance."""
from copy import deepcopy
import json
from pathlib import Path
import runpy
import unittest

from evals.auto_research_assessment import assessment_evidence


class AssessmentAcceptanceTests(unittest.TestCase):
    def test_receipts_request_and_following_action_are_required(self):
        def message(call_id, value):
            return {'role': 'tool', 'tool_call_id': call_id,
                    'content': json.dumps({'UNTRUSTED_TOOL_DATA': value})}
        values = [{'name': role, 'role': role, 'valid': True, 'metrics': {'score': score},
                   'script_sha256': 'fixed', 'result': {'config': {'dataset': 'test', 'dataset_version': '1',
                                                              'split': 'dev', 'seeds': [13]}}}
                  for role, score in [('baseline', .6), ('candidate', .4)]]
        tasks = [{'id': 'task', 'request': {'plan': json.dumps({'version': 1})},
                  'state': {'measurements': values}}]
        targets = [{'name': 'score', 'direction': 'higher', 'value': .8}]
        assessment = {'call_id': 'assess', 'plan_version': 1, 'recommendation': 'continue_research',
                      'comparable_pairs': [{'task_id': 'task', 'baseline_task_id': 'task', 'candidate': 'candidate',
                                            'baseline': 'baseline', 'candidate_metrics': {'score': .4},
                                            'baseline_metrics': {'score': .6}}]}
        requests = [{'purpose': 'auto_research', 'messages': [message('run', {'id': 'task', 'measurements': values})],
                     'response': {'kind': 'tool_call', 'tool_name': 'assess_results', 'call_id': 'assess',
                                  'arguments': {'plan_version': 1}}},
                    {'purpose': 'auto_research', 'messages': [message('assess', assessment)],
                     'response': {'kind': 'tool_call', 'tool_name': 'research'}}]
        self.assertTrue(assessment_evidence(requests, tasks, [assessment], targets)['passed'])
        self.assertFalse(assessment_evidence(requests, tasks, [], targets)['passed'])
        # An early request for missing measurements is valid, but cannot itself
        # satisfy the final measured-plan coverage requirement.
        early = {'call_id': 'early', 'plan_version': 1, 'recommendation': 'collect_measurements',
                 'measurement_count': 0, 'comparable_pairs': []}
        prefix = [{'purpose': 'auto_research', 'messages': [], 'response': {
                    'kind': 'tool_call', 'tool_name': 'assess_results', 'call_id': 'early',
                    'arguments': {'plan_version': 1}}},
                  {'purpose': 'auto_research', 'messages': [message('early', early)],
                   'response': {'kind': 'tool_call', 'tool_name': 'run_experiments'}}]
        self.assertTrue(assessment_evidence(prefix + requests, tasks, [early, assessment], targets)['passed'])
        self.assertFalse(assessment_evidence(prefix, tasks, [early], targets)['passed'])

        blocked = {**early, 'call_id': 'blocked', 'recommendation': 'blocked', 'measurement_count': 1}
        blocked_prefix = deepcopy(prefix)
        blocked_prefix[0]['messages'] = [message('partial', {'id': 'task', 'measurements': values[1:]})]
        blocked_prefix[0]['response']['call_id'] = 'blocked'
        blocked_prefix[1]['messages'] = [message('blocked', blocked)]
        self.assertTrue(assessment_evidence(blocked_prefix + requests, tasks, [blocked, assessment], targets)['passed'])
        # Once a real pair was received, an empty assessment cannot hide it.
        hidden = {**early, 'measurement_count': 2}
        hidden_prefix = deepcopy(prefix)
        hidden_prefix[0]['messages'] = [message('full', {'id': 'task', 'measurements': values})]
        hidden_prefix[1]['messages'] = [message('early', hidden)]
        self.assertFalse(assessment_evidence(hidden_prefix + requests, tasks, [hidden, assessment], targets)['passed'])

        for failure in ('no_call', 'no_feedback', 'not_consumed', 'fabricated_score', 'false_goal', 'wrong_conditions'):
            req, ts, assessments = deepcopy(requests), deepcopy(tasks), [deepcopy(assessment)]
            if failure == 'no_call': req[0]['response']['tool_name'] = 'inspect'
            if failure == 'no_feedback': req[0]['messages'] = []
            if failure == 'not_consumed': req = req[:1]
            if failure == 'fabricated_score': assessments[0]['comparable_pairs'][0]['candidate_metrics']['score'] = .9
            if failure == 'false_goal': assessments[0]['recommendation'] = 'goal_met'
            if failure == 'wrong_conditions': ts[0]['state']['measurements'][1]['script_sha256'] = 'different'
            with self.subTest(failure=failure):
                self.assertFalse(assessment_evidence(req, ts, assessments, targets)['passed'])
        # The live tool's no-target and goal-met recommendations are independently checked.
        for score, goal, recommendation in [(.9, targets, 'goal_met'), (.7, targets, 'revise_method'), (.4, [], 'review_required')]:
            req, ts, assessed = deepcopy(requests), deepcopy(tasks), deepcopy(assessment)
            ts[0]['state']['measurements'][1]['metrics']['score'] = score
            assessed['comparable_pairs'][0]['candidate_metrics']['score'] = score
            assessed['recommendation'] = recommendation
            req[0]['messages'] = [message('run', {'id': 'task', 'measurements': ts[0]['state']['measurements']})]
            req[1]['messages'] = [message('assess', assessed)]
            self.assertTrue(assessment_evidence(req, ts, [assessed], goal)['passed'])

    def test_missing_pair_assessment_uses_full_receipts_when_feedback_is_compressed(self):
        from research_agent.auto_research import AutoResearch
        def message(call, value):
            return {'role': 'tool', 'tool_call_id': call,
                    'content': json.dumps({'UNTRUSTED_TOOL_DATA': value})}
        config = {'dataset': 'd', 'dataset_version': 'v', 'split': 'dev', 'seeds': [13]}
        baselines = [{'name': f'base-{i}', 'role': 'baseline', 'valid': True,
                      'metrics': {'score': .4}, 'script_sha256': 'same', 'result': {'config': config}}
                     for i in range(9)]
        pair = [{**baselines[0], 'name': 'baseline'}, {**baselines[0], 'name': 'candidate', 'role': 'candidate'}]
        tasks = [{'id': ident, 'status': 'completed', 'request': {'plan': json.dumps({'version': 1})},
                  'state': {'measurements': values}} for ident, values in [('early-task', baselines), ('later-task', pair)]]
        early = {'call_id': 'early', 'plan_version': 1, 'measurement_count': 9,
                 'recommendation': 'collect_measurements', 'comparable_pairs': []}
        late = {'call_id': 'late', 'plan_version': 1, 'recommendation': 'continue_research',
                'comparable_pairs': [{'task_id': 'later-task', 'baseline_task_id': 'later-task',
                    'candidate': 'candidate', 'baseline': 'baseline',
                    'candidate_metrics': {'score': .4}, 'baseline_metrics': {'score': .4}}]}
        compact = AutoResearch.context_result({'id': 'early-task', 'status': 'completed', 'measurements': baselines})
        self.assertTrue(compact['_context_truncated'])
        requests = [
            {'purpose': 'auto_research', 'messages': [message('run-early', compact)], 'response': {
                'kind': 'tool_call', 'tool_name': 'assess_results', 'call_id': 'early', 'arguments': {'plan_version': 1}}},
            {'purpose': 'auto_research', 'messages': [message('early', early)], 'response': {
                'kind': 'tool_call', 'tool_name': 'run_experiments'}},
            {'purpose': 'auto_research', 'messages': [message('run-late', {'id': 'later-task', 'measurements': pair})],
             'response': {'kind': 'tool_call', 'tool_name': 'assess_results', 'call_id': 'late', 'arguments': {'plan_version': 1}}},
            {'purpose': 'auto_research', 'messages': [message('late', late)],
             'response': {'kind': 'tool_call', 'tool_name': 'finish_research'}}]
        targets = [{'name': 'score', 'direction': 'higher', 'value': .8}]
        self.assertTrue(assessment_evidence(requests, tasks, [early, late], targets)['passed'])
        requests[0]['messages'] = []
        self.assertFalse(assessment_evidence(requests, tasks, [early, late], targets)['passed'])

    def test_decisive_pair_survives_saved_and_model_feedback_limits(self):
        from types import SimpleNamespace
        from research_agent.auto_research import AutoResearch
        tasks = []
        for index, count in enumerate((23, 23, 15)):
            values = [{'name': 'baseline', 'role': 'baseline', 'metrics': {'score': .2}}] + [
                {'name': f'candidate-{i}', 'role': 'candidate',
                 'metrics': {'score': .9 if index == 2 and i == 14 else .4}} for i in range(count)]
            for m in values:
                m.update(valid=True, script_sha256='fixed', result={'config': {
                    'dataset': 'test', 'dataset_version': '1', 'split': 'dev', 'seeds': [13]}})
            tasks.append({'id': str(index), 'job_id': 'job', 'status': 'completed',
                          'request': {'plan': json.dumps({'version': 1})},
                          'state': {'after': {'evaluate.py': 'fixed'}, 'measurements': values}})
        app = SimpleNamespace(store=None, coding=SimpleNamespace(get=lambda space, ident: tasks[int(ident)]))
        auto = AutoResearch(app)
        targets = [{'name': 'score', 'direction': 'higher', 'value': .8}]
        assessment = auto.assess_results({'id': 'job', 'space_id': 'space'},
            {'plans': [{'version': 1}], 'coding': ['0', '1', '2']}, {'targets': targets}, 1)
        self.assertEqual(assessment['recommendation'], 'goal_met')
        self.assertLessEqual(len(assessment['comparable_pairs']), 60)
        compact = AutoResearch.context_result(assessment)
        self.assertEqual(compact['comparable_pairs'][0]['candidate_metrics']['score'], .9)
        def message(call, value):
            return {'role': 'tool', 'tool_call_id': call,
                    'content': json.dumps({'UNTRUSTED_TOOL_DATA': value})}
        assessment['call_id'] = 'assess'
        requests = [{'purpose': 'auto_research', 'messages': [message(t['id'], {
            'id': t['id'], 'measurements': t['state']['measurements']}) for t in tasks],
            'response': {'kind': 'tool_call', 'tool_name': 'assess_results',
                         'call_id': 'assess', 'arguments': {'plan_version': 1}}},
            {'purpose': 'auto_research', 'messages': [message('assess', compact)],
             'response': {'kind': 'tool_call', 'tool_name': 'finish_research'}}]
        self.assertTrue(assessment_evidence(requests, tasks, [assessment], targets)['passed'])

    def test_frozen_scoring_distinguishes_partial_recall_from_hit_rate(self):
        from evals.audit_auto_research_results import score_rankings
        tasks = [{'id': 'q', 'source_id': 'p', 'gold_context_ids': ['a', 'b']}]
        corpus = [{'id': ident, 'source_id': 'p'} for ident in ('a', 'b', 'x')]
        rows = [{'seed': 13, 'task_id': 'q', 'rankings': [{'paragraph_id': 'x'}, {'paragraph_id': 'a'}]}]
        result = score_rankings(tasks, corpus, rows, [13])
        self.assertEqual(result['metrics'], {'recall_at_5': .5, 'mrr_at_5': .5, 'hit_at_5': 1})
        with self.assertRaises(ValueError):
            score_rankings(tasks, corpus, [{'seed': 13, 'task_id': 'q',
                                            'rankings': [{'paragraph_id': 'a'}, {'paragraph_id': 'a'}]}], [13])
