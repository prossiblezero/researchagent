"""Research feedback contains inspectable failures, without host-only labels."""
import copy
import hashlib
import json
from contextlib import closing
from unittest.mock import patch
import unittest

from research_agent.auto_research import AutoResearch
from research_agent.locomo_metrics import audit_locomo_result
import tests.test_coding_tool as fixtures
import tests.test_measurement_feedback as feedback_fixtures


class FailureDiagnosticsTests(unittest.TestCase):
    setUp = fixtures.CodingToolTests.setUp

    def prepare(self, perfect=False):
        workspace = self.tool.work_root / self.job['id']
        workspace.mkdir(parents=True)
        (workspace / 'evaluate.py').write_text('pass', encoding='utf-8')
        inputs = {
            'tasks': [{'id': 'q', 'conversation_id': 'c', 'split': 'development', 'category': 1,
                       'question': 'When did the hike happen?'}],
            'corpus': [{'conversation_id': 'c', 'split': 'development', 'conversation': {
                'session_1': [{'dia_id': 'D1:1'}, {'dia_id': 'D1:2'}],
                'session_2': [{'dia_id': 'D2:1'}]}}]}
        for name, data in inputs.items():
            (workspace / (name + '.json')).write_text(json.dumps(data), encoding='utf-8')
        private = self.store.path.parent / 'auto-research' / self.job['id']
        private.mkdir(parents=True)
        labels = private / 'scoring-labels.json'
        labels.write_text(json.dumps([{'id': 'q', 'answer': 'HOST_ONLY_LABEL',
            'gold_evidence': ['D2:1'], 'retrieval_scorable': True}]), encoding='utf-8')
        contract = {'name': 'locomo_qa_v1', 'seeds': [13], 'tasks_path': 'tasks.json', 'corpus_path': 'corpus.json',
            'labels_sha256': hashlib.sha256(labels.read_bytes()).hexdigest(),
            **{name + '_sha256': hashlib.sha256((workspace / (name + '.json')).read_bytes()).hexdigest() for name in inputs}}
        with closing(self.store._connect()) as db, db:
            payload = json.loads(db.execute('select payload from research_jobs where id=?', (self.job['id'],)).fetchone()[0])
            payload['auto_research']['metric_contract'] = contract
            db.execute('update research_jobs set payload=? where id=?', (json.dumps(payload), self.job['id']))
        prediction = {'task_id': 'q', 'seed': 13,
            'answer': 'HOST_ONLY_LABEL' if perfect else 'Last week.',
            'evidence_ids': ['D2:1'] if perfect else ['D1:2'],
            'question': 'FAKE_SCRIPT_QUESTION', 'gold_evidence': ['FORGED']}
        (workspace / 'predictions.jsonl').write_text(json.dumps(prediction) + '\n', encoding='utf-8')
        result = {'metrics': {'answer_f1': 1},
            'config': {'dataset': 'locomo', 'dataset_version': contract['tasks_sha256'], 'split': 'development', 'seeds': [13]},
            'diagnostics': {'predictions_path': 'predictions.jsonl', 'failure_examples': [{'question': 'FAKE_SCRIPT_QUESTION'}],
                            'input_summary': {'total_turns': 999}}}
        (workspace / 'result.json').write_text(json.dumps(result), encoding='utf-8')
        return workspace, labels, contract, result

    def test_host_receipt_keeps_question_prediction_and_scale_without_gold(self):
        workspace, labels, contract, result = self.prepare()
        task = self.tool.submit(self.job, 'diagnostics', {**self.request, 'execution_only': True, 'seconds': 0, 'token_budget': 0})
        def runner(*args, **kwargs):
            (workspace / 'result.json').write_text(json.dumps(result), encoding='utf-8')
            return {'exit_code': 0, 'termination': 'completed', 'seconds': .1, 'stdout': '', 'stderr': ''}
        with patch('research_agent.coding_tool.sandbox_command', return_value=['sandbox']), \
             patch('research_agent.coding_tool.run_process', side_effect=runner):
            self.tool.execute(self.space, task['id'])
        measurement, = self.tool.get(self.space, task['id'])['state']['measurements']
        self.assertTrue(measurement['valid'])
        compact = AutoResearch.context_result({'measurements': [measurement]})
        measured, = compact['measurements']
        example, = measured['failure_examples']
        self.assertEqual(example['question'], 'When did the hike happen?')
        self.assertEqual(example['prediction'], 'Last week.')
        self.assertEqual(example['retrieved_evidence_ids'], ['D1:2'])
        self.assertEqual(measured['input_summary']['total_turns'], 3)
        self.assertEqual(measured['input_summary']['conversation_turns'], {'c': 3})
        self.assertEqual(measured['input_summary']['task_count'], 1)
        self.assertEqual(measured['metrics']['answer_f1'], 0)
        public = json.dumps({'examples': measured['failure_examples'], 'inputs': measured['input_summary']})
        for hidden in ['HOST_ONLY_LABEL', 'gold_evidence', 'FORGED', 'FAKE_SCRIPT_QUESTION', 'D2:1']:
            self.assertNotIn(hidden, public)

    def test_perfect_predictions_are_not_labelled_failures(self):
        workspace, labels, contract, result = self.prepare(perfect=True)
        _, audit = audit_locomo_result(workspace, result, contract, labels)
        self.assertEqual(audit['failure_examples'], [])
        self.assertEqual(audit['input_summary']['origin'], 'host_frozen_inputs')

    def test_three_roles_keep_diagnostic_text_and_input_scale_inside_budget(self):
        raw = feedback_fixtures.feedback()
        seed = raw['measurements'][0]
        for example in seed['failure_examples']:
            example.update(question='What happened? ' * 100, prediction='Last week. ' * 200,
                           retrieved_evidence_ids=['D1:' + str(i) for i in range(10)])
        seed['input_summary'] = {'origin': 'host_frozen_inputs', 'task_count': 40,
            'conversation_turns': {'conv-26': 419, 'conv-30': 369}, 'total_turns': 788}
        raw['measurements'] = [{**copy.deepcopy(seed), 'role': role, 'name': role}
                               for role in ['baseline', 'candidate', 'ablation']]
        before = copy.deepcopy(raw)
        compact = AutoResearch.context_result(raw)
        self.assertLessEqual(len(json.dumps(compact, ensure_ascii=False)), 7000)
        self.assertEqual(raw, before)
        self.assertEqual([m['role'] for m in compact['measurements']], ['baseline', 'candidate', 'ablation'])
        for measurement in compact['measurements']:
            self.assertEqual(measurement['input_summary']['total_turns'], 788)
            self.assertEqual(measurement['metrics'], seed['metrics'])
            examples = measurement['failure_examples']
            self.assertEqual([r['task_id'] for r in examples], [r['task_id'] for r in seed['failure_examples']])
            self.assertTrue(examples[0]['question'].startswith('What happened?'))
            self.assertTrue(examples[0]['prediction'].startswith('Last week.'))
            self.assertTrue(examples[0]['retrieved_evidence_ids'])


    def test_large_conversation_summary_cannot_displace_measurements(self):
        raw = feedback_fixtures.feedback()
        seed = raw['measurements'][0]
        counts = {'conversation-' + str(i): 400 for i in range(200)}
        seed['input_summary'] = {'origin': 'host_frozen_inputs', 'task_count': 40,
            'conversation_count': 200, 'conversation_turns': counts, 'total_turns': 80000}
        raw['measurements'] = [{**copy.deepcopy(seed), 'role': role, 'name': role}
                               for role in ['baseline', 'candidate', 'ablation']]
        compact = AutoResearch.context_result(raw)
        self.assertLessEqual(len(json.dumps(compact, ensure_ascii=False)), 7000)
        self.assertEqual([m['role'] for m in compact['measurements']], ['baseline', 'candidate', 'ablation'])
        for item in compact['measurements']:
            self.assertEqual(item['metrics'], seed['metrics'])
            self.assertEqual(item['input_summary']['total_turns'], 80000)
            self.assertEqual(item['input_summary']['conversation_count'], 200)
            self.assertLessEqual(len(item['input_summary'].get('conversation_turns', {})), 8)
        self.assertEqual(len(seed['input_summary']['conversation_turns']), 200)
        raw['source_changes'] = ['large-path-' + ('x' * 600) for _ in range(8)]
        compact = AutoResearch.context_result(raw)
        for item in compact['measurements']:
            summary = item['input_summary']
            self.assertEqual(summary['conversation_details_omitted'], 200 - len(summary.get('conversation_turns', {})))


if __name__ == '__main__':
    unittest.main()
