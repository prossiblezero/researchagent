"""Real host feedback must survive compression into the next model request."""
import copy
from dataclasses import asdict
import json
from pathlib import Path
import unittest

from research_agent.auto_research import AutoResearch
import tests.test_auto_research as fixtures

FIXTURE = Path(__file__).parent / 'fixtures/amem-full-baseline-feedback.json'

def feedback():
    return json.loads(FIXTURE.read_text(encoding='utf-8'))


class MeasurementFeedbackTests(unittest.TestCase):
    setUp = fixtures.AutoResearchTests.setUp
    agent = fixtures.AutoResearchTests.agent

    def test_actual_feedback_reaches_model_after_sqlite_checkpoint(self):
        raw = feedback(); before = copy.deepcopy(raw)
        job = self.app.auto_research.enqueue(self.space, self.chat, 'Research the measured failures')
        job = self.store.claim_next(); state = self.app.auto_research.state(job)
        state['pending'] = asdict(fixtures.call('inspect', {'kind': 'coding', 'id': raw['id']}))
        self.app.auto_research.complete_tool(job, state, raw)
        saved = self.app.auto_research.state(job)
        wrapped = saved['messages'][-1]['content']
        compact = json.loads(wrapped)['UNTRUSTED_TOOL_DATA']
        self.assertLessEqual(len(wrapped), 7030)
        row, = compact['measurements']
        original = raw['measurements'][0]
        self.assertEqual(row['metrics'], original['metrics'])
        self.assertEqual(row['failure_examples'], original['failure_examples'])
        self.assertEqual(row['result']['config']['dataset_version'], original['result']['config']['dataset_version'])
        self.assertTrue(row['valid'])
        self.assertEqual(raw, before)
        self.model.complete.return_value = fixtures.call('inspect', {'kind': 'coding', 'id': raw['id']}, 2)
        self.app.auto_research.decide(job, saved, json.loads(job['payload'])['auto_research'], lambda *a, **kw: None)
        messages = self.model.complete.call_args.args[0]
        tool = next(m for m in messages if m.get('tool_call_id') == 'call-1')
        self.assertEqual(json.loads(tool['content'])['UNTRUSTED_TOOL_DATA']['measurements'][0]['failure_examples'], original['failure_examples'])

    def test_three_large_receipts_keep_roles_metrics_and_failure_ids(self):
        raw = feedback(); seed = raw['measurements'][0]
        raw['measurements'] = []
        for role in ('baseline', 'candidate', 'ablation'):
            item = copy.deepcopy(seed); item['name'] = role; item['role'] = role
            raw['measurements'].append(item)
        compact = AutoResearch.context_result(raw)
        self.assertLessEqual(len(json.dumps(compact, ensure_ascii=False)), 7000)
        self.assertEqual([m['role'] for m in compact['measurements']], ['baseline', 'candidate', 'ablation'])
        for row in compact['measurements']:
            self.assertEqual(row['metrics'], seed['metrics'])
            self.assertEqual(row['failure_examples'], seed['failure_examples'])
        raw['measurements'][1]['valid'] = False
        raw['measurements'][1]['error'] = 'synthetic real execution failure'
        compact = AutoResearch.context_result(raw)
        failed = next(row for row in compact['measurements'] if row['role'] == 'candidate')
        self.assertFalse(failed['valid'])
        self.assertEqual(failed['error'], 'synthetic real execution failure')


if __name__ == '__main__':
    unittest.main()
