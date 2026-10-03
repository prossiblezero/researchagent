"""Only host-recorded generation settings can support automatic result pairing."""
import copy
import json
from contextlib import closing
import unittest

import tests.test_host_comparison as fixtures
from evals.auto_research_assessment import assessment_evidence


class GenerationComparisonTests(unittest.TestCase):
    setUp = fixtures.HostComparisonTests.setUp
    agent = fixtures.HostComparisonTests.agent
    state = fixtures.HostComparisonTests.state
    setup_run = fixtures.HostComparisonTests.setup_run

    def persist(self, tasks):
        for task in tasks:
            self.app.coding.save(task['id'], task['state'], 'completed')

    def test_explicit_mismatch_blocks_goal_and_v3_even_when_script_claims_match(self):
        job, state, tasks = self.setup_run()
        for index, task in enumerate(tasks):
            for response in task['state']['model_requests']:
                response['generation_parameters'] = {'temperature': .7 if index == 0 else .3, 'max_tokens': 1000}
        baseline = self.app.auto_research.measurements_for_comparison(tasks[0])[0]
        for measurement in tasks[1]['state']['measurements']:
            measurement['comparison_contract'] = copy.deepcopy(baseline['comparison_contract'])
            measurement['result']['config']['parameters'] = {'generation_parameters': {'temperature': .7, 'max_tokens': 1000}}
        self.persist(tasks)
        result = self.app.auto_research.assess_results(job, state, {'targets':[{'name':'answer_f1', 'direction':'higher', 'value':.8}]}, 2)
        self.assertEqual(result['recommendation'], 'blocked')
        self.assertEqual(result['comparable_pairs'], [])
        summary = self.app.auto_research.measurement_summary(job, state)
        with closing(self.store._connect()) as db, db:
            imported = self.app.records.import_auto_measurements(db, job, summary)
        baseline_id = next(m['version_id'] for m in imported['imported'] if m['role'] == 'baseline')
        candidate_id = next(m['version_id'] for m in imported['imported'] if m['role'] == 'candidate')
        compared = self.app.records.compare_experiments(self.space, baseline_id, candidate_id)
        self.assertFalse(compared['comparable'])
        self.assertIn('host_scoring_contract', compared['mismatches'])

    def test_per_measurement_parameter_sets_ignore_order_duplicates_and_numeric_spelling(self):
        job, state, tasks = self.setup_run()
        sample = copy.deepcopy(tasks[0]['state']['model_requests'][0])
        tasks[0]['state']['model_requests'] = [{**sample, 'generation_parameters': {'temperature': t, 'max_tokens': 1000}} for t in (1, .3, 1)]
        tasks[0]['state']['measurements'][0]['host_model_request_range'] = [0, 3]
        tasks[1]['state']['model_requests'] = [{**sample, 'generation_parameters': {'temperature': t, 'max_tokens': 1000}} for t in (.3, 1.0, .9)]
        tasks[1]['state']['measurements'][0]['host_model_request_range'] = [0, 2]
        tasks[1]['state']['measurements'][1]['host_model_request_range'] = [2, 3]
        self.persist(tasks)
        controller = self.app.auto_research
        baseline = controller.measurements_for_comparison(tasks[0])[0]
        candidate, ablation = controller.measurements_for_comparison(tasks[1])
        self.assertTrue(controller.comparable(candidate, baseline))
        self.assertFalse(controller.comparable(ablation, baseline))
        settings = baseline['comparison_contract']['requested_generation_parameter_sets']
        self.assertEqual(len(settings), 2)
        self.assertEqual({row['temperature'] for row in settings}, {.3, 1})
        self.assertEqual(controller.assess_results(job, state, {'targets':[{'name':'answer_f1', 'direction':'higher', 'value':.8}]}, 2)['recommendation'], 'goal_met')

    def test_legacy_identity_unchanged_and_invalid_or_partly_unknown_settings_rejected(self):
        _, _, tasks = self.setup_run()
        controller = self.app.auto_research
        baseline = controller.measurements_for_comparison(tasks[0])[0]
        self.assertEqual(set(baseline['comparison_contract']), {'job_id', 'scoring', 'requested_model_id', 'actual_models', 'transport'})
        self.assertTrue(controller.comparable(controller.measurements_for_comparison(tasks[1])[0], baseline))
        for parameters in ({'temperature': .7}, {'max_tokens': 1000}):
            changed = copy.deepcopy(tasks[1]); changed['state']['model_requests'][0]['generation_parameters'] = parameters
            with self.subTest(parameters=parameters):
                self.assertFalse(controller.comparable(controller.measurements_for_comparison(changed)[0], baseline))
        for invalid in (None, [], {'temperature': True}, {'temperature': float('nan')}, {'temperature': 10**400},
                        {'max_tokens': 0}, {'max_tokens': 1000.0}, {'base_url': 'https://invalid.example'}):
            changed = copy.deepcopy(tasks[1]); changed['state']['model_requests'][0]['generation_parameters'] = invalid
            with self.subTest(invalid=invalid):
                self.assertIsNone(controller.measurements_for_comparison(changed)[0]['comparison_contract'])

    def test_acceptance_does_not_confirm_a_pair_with_changed_request_cap(self):
        job, state, tasks = self.setup_run()
        for task in tasks:
            for response in task['state']['model_requests']:
                response['generation_parameters'] = {'temperature': .7, 'max_tokens': 1000}
        self.persist(tasks)
        result = self.app.auto_research.assess_results(job, state, {'targets':[]}, 2)
        result['call_id'] = 'assess-2'
        request = {'purpose':'auto_research','response':{'kind':'tool_call','tool_name':'assess_results',
                   'call_id':'assess-2','arguments':{'plan_version':2}},
                   'messages':[{'role':'tool','tool_call_id':'result-'+task['id'],
                       'content':json.dumps({'UNTRUSTED_TOOL_DATA':{'id':task['id'], 'status':'completed',
                                           'measurements':task['state']['measurements']}})} for task in tasks]}
        following = {'purpose':'auto_research','response':{'kind':'tool_call','tool_name':'finish_research'},
            'messages':[{'role':'tool','tool_call_id':'assess-2','content':json.dumps({'UNTRUSTED_TOOL_DATA':result})}]}
        self.assertTrue(assessment_evidence([request, following], tasks, [result], [])['passed'])
        tasks[1]['state']['model_requests'][0]['generation_parameters']['max_tokens'] = 2000
        self.assertFalse(assessment_evidence([request, following], tasks, [result], [])['passed'])


if __name__ == '__main__':
    unittest.main()
