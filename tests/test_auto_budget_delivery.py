"""A bounded controller must leave its last model decision for delivery."""
import json
import unittest
from tests import test_auto_research as support
from research_agent.auto_research import TOOLS


class BudgetDeliveryTests(unittest.TestCase):
    def test_final_decision_delivers_observed_findings_within_original_budget(self):
        case = support.AutoResearchTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        job = case.start(budget={'decisions': 2})
        workspace = case.app.coding.work_root / job['id']
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / 'source.txt').write_text('Measured input, not a completed experiment.', encoding='utf-8')
        offered = []
        def choose(messages, tools):
            names = [tool['function']['name'] for tool in tools]
            offered.append(names)
            if 'inspect' in names:
                return support.call('inspect', {'kind':'file','id':'current','path':'source.txt','offset':0}, len(offered))
            self.assertTrue(any('Measured input' in str(message) for message in messages))
            self.assertIn('最后一次', messages[-1]['content'])
            return support.call('finish_research', {'outcome':'budget_exhausted',
                'summary':'source.txt contains: Measured input, not a completed experiment.',
                'limitations':'No experiment executed; objective remains unverified.'}, len(offered))
        case.model.complete.side_effect = choose
        for _ in range(5):
            current = case.store.job(job['space_id'], job['id'])
            if current['status'] in {'completed','failed'}:
                break
            case.app.auto_research.execute(case.store.claim_next())
        final = case.store.job(job['space_id'], job['id'])
        self.assertEqual(final['status'], 'completed')
        self.assertEqual(case.model.complete.call_count, 2)
        self.assertEqual(offered[0], [tool['function']['name'] for tool in TOOLS])
        self.assertEqual(offered[-1], ['finish_research'])
        state = case.state(final)
        self.assertEqual(state['outcome'], 'budget_exhausted')
        self.assertIn('Measured input', state['summary'])
        self.assertIn('unverified', state['limitations'])
        self.assertEqual((workspace / 'source.txt').read_text(), 'Measured input, not a completed experiment.')

    def test_final_delivery_cannot_claim_goal_without_host_assessment(self):
        case = support.AutoResearchTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        job = case.start(budget={'decisions': 1})
        def choose(messages, tools):
            return support.call('finish_research', {'outcome':'goal_met','summary':'Unsupported success','limitations':''}, 1)
        case.model.complete.side_effect = choose
        case.app.auto_research.execute(case.store.claim_next())
        case.app.auto_research.execute(case.store.claim_next())
        final = case.store.job(job['space_id'], job['id'])
        state = case.state(final)
        self.assertEqual(case.model.complete.call_count, 1)
        self.assertEqual([tool['function']['name'] for tool in case.model.complete.call_args.args[1]], ['finish_research'])
        self.assertEqual(state['outcome'], 'budget_exhausted')
        self.assertNotEqual(state['summary'], 'Unsupported success')


if __name__ == '__main__':
    unittest.main()
