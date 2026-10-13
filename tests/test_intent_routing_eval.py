import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from evals import run_intent_routing_eval as ev
from research_agent.contracts import ModelDecision
from research_agent.models import OpenAICompatibleModel


class IntentRoutingEvaluationTests(unittest.TestCase):
    def test_frozen_panel_and_split_leakage(self):
        panel = ev.read(ev.PANEL)
        ev.validate(panel)
        self.assertEqual(sum(c['origin']['kind'] == 'MASSIVE' for c in panel['cases']), 32)
        altered = copy.deepcopy(panel)
        dev = next(c for c in altered['cases'] if c['split'] == 'dev')
        test = next(c for c in altered['cases'] if c['split'] == 'test')
        test['group'] = dev['group']
        with self.assertRaisesRegex(ValueError, 'Group crosses'):
            ev.validate(altered)
        altered = copy.deepcopy(panel)
        altered['cases'][1]['expected_intent'] = 'UNKNOWN'
        with self.assertRaisesRegex(ValueError, 'unknown label'):
            ev.validate(altered)

    def test_published_results_reproduce_original_and_audited_scores(self):
        panel = ev.read(ev.PANEL)
        evidence = ev.read(ev.ROOT/'docs/research/intent-routing-results-20261012.json')
        normalized = (json.dumps(panel, ensure_ascii=False, indent=2)+'\n').encode()
        self.assertEqual(ev.sha(normalized), evidence['audited_panel_sha256'])
        original = copy.deepcopy(panel)
        audit = original.pop('label_audit')
        original['limits'] = audit['previous_limits']
        by_id = {case['id']: case for case in original['cases']}
        self.assertEqual(len(audit['corrections']), 4)
        for correction in audit['corrections']:
            by_id[correction['id']]['expected_intent'] = correction['before']
            by_id[correction['id']]['rationale'] = correction['previous_rationale']
        # The preserved Windows run serialized the original panel with CRLF.
        frozen_bytes = (json.dumps(original, ensure_ascii=False, indent=2)+'\n').replace('\n', '\r\n').encode()
        self.assertEqual(ev.sha(frozen_bytes), evidence['protocol']['panel_sha256'])
        rows = evidence['rows']
        self.assertEqual(len(rows), 210)
        self.assertEqual(sum(row['attempts'] for row in rows), 211)
        self.assertEqual(sum(bool(row['error']) for row in rows), 4)
        for dataset, key in [(original, 'original_metrics'), (panel, 'audited_metrics')]:
            cases = [case for case in dataset['cases'] if case['split'] == 'test']
            computed = ev.score(cases, rows)
            for metric, value in computed.items():
                self.assertEqual(value, evidence[key][metric], (key, metric))
        self.assertEqual(evidence['original_metrics']['correct'], 193)
        self.assertEqual(evidence['audited_metrics']['correct'], 197)

    def test_failures_missing_and_wrong_labels_stay_in_denominator(self):
        cases = [dict(id=str(i), expected_intent=label, origin={'kind': 'authored'}, tags=[])
                 for i, label in enumerate(['CHAT', 'CHAT', 'RESEARCH', 'AUTO_RESEARCH'])]
        rows = [{'id': '0', 'response': {'intent': 'CHAT'}},
                {'id': '1', 'response': {'intent': 'AUTO_RESEARCH'}},
                {'id': '2', 'response': {'intent': 'CHAT'}}]
        result = ev.score(cases, rows)
        self.assertEqual((result['correct'], result['n']), (1, 4))
        self.assertEqual(result['accuracy'], .25)
        self.assertAlmostEqual(result['macro_f1'], .5/7)
        self.assertEqual(result['auto_research_false_positive_rate'], 1/3)
        self.assertEqual(result['errors_or_missing'], 1)
        self.assertEqual(result['confusion']['AUTO_RESEARCH']['ERROR'], 1)
        rows.append({'id': '3', 'response': {'intent': 'AUTO_RESEARCH'}, 'error': 'invalid schema'})
        self.assertEqual(ev.score(cases, rows)['correct'], 1)
        with self.assertRaisesRegex(ValueError, 'Duplicate or unknown'):
            ev.score(cases, rows + [rows[0]])
        with self.assertRaisesRegex(ValueError, 'empty'):
            ev.score([], [])

    def test_raw_classification_uses_production_parser_and_never_sends_gold(self):
        observed = []
        class Provider(OpenAICompatibleModel):
            def _complete(self, messages, tools):
                observed.append(copy.deepcopy(messages))
                self.last_usage = {'input_tokens': 9, 'output_tokens': 1, 'total_tokens': 10}
                return ModelDecision('final', content=json.dumps({'intent': 'CHAT', 'reply': 'Hello',
                    'brief': '', 'assumptions': [], 'effort': 'none'}))
        case = copy.deepcopy(next(c for c in ev.read(ev.PANEL)['cases'] if c['split'] == 'test'))
        case['rationale'] = 'GOLD_SENTINEL'
        case['expected_intent'] = 'AUTO_RESEARCH'
        case['tags'].append('TAG_SENTINEL')
        budget = ev.Budget({'seconds': 60, 'requests': 1, 'tokens': 100000})
        with tempfile.TemporaryDirectory() as tmp, patch.object(ev, 'model_from_env', return_value=Provider('https://invalid.example', 'secret', 'fake')):
            row = ev.evaluate_one(case, Path(tmp), 'fake', budget)
            self.assertEqual(row['response']['intent'], 'CHAT')
            self.assertEqual(row['attempts'], 1)
            self.assertEqual(row['usage'][0]['total_tokens'], 10)
            sent = json.dumps(observed)
            self.assertNotIn('GOLD_SENTINEL', sent)
            self.assertNotIn('TAG_SENTINEL', sent)
            self.assertNotIn('expected_intent', sent)
            request = ev.read(Path(tmp)/'cases'/case['id']/'requests/001.json')
            self.assertEqual(request['tools'], [])
            self.assertEqual(request['messages'][0]['content'], ev.ROUTER_PROMPT)
            with self.assertRaises(FileExistsError):
                ev.evaluate_one(case, Path(tmp), 'fake', budget)

    def test_shared_budget_reserves_pending_and_unknown_usage(self):
        limits = {'seconds': 60, 'requests': 2, 'tokens': 11000}
        budget = ev.Budget(limits)
        first = budget.reserve([], [])
        second = budget.reserve([], [])
        with self.assertRaisesRegex(RuntimeError, 'budget exhausted'):
            budget.reserve([], [])
        budget.finish(first, {'total_tokens': 10})
        budget.finish(second, None)
        state = budget.snapshot()
        self.assertEqual(state['unknown_usage_attempts'], 1)
        self.assertEqual(state['accounted_tokens_including_unknown_reservations'], 10+second)
        self.assertEqual(state['pending_reserved_tokens'], 0)
        budget = ev.Budget({**limits, 'requests': 10, 'tokens': first*2-1})
        budget.reserve([], [])
        with self.assertRaisesRegex(RuntimeError, 'budget exhausted'):
            budget.reserve([], [])
        budget.deadline = 0
        with self.assertRaisesRegex(RuntimeError, 'budget exhausted'):
            budget.reserve([], [])

    def test_completed_results_are_aggregated_once_without_replacing_live_file(self):
        aggregate_writes = []
        visited = []
        original_write = ev.write
        def fragile_windows_write(path, value):
            if Path(path).name == 'results.json':
                aggregate_writes.append(path)
                if len(aggregate_writes) > 1:
                    raise PermissionError('Windows destination temporarily held open')
            original_write(path, value)
        def classify(case, *args):
            visited.append(case['id'])
            return {'id': case['id'], 'response': {'intent': 'CHAT'}, 'error': None,
                    'attempts': 1, 'seconds': 1, 'usage': []}
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(ev, 'model_from_env', return_value=OpenAICompatibleModel('https://invalid.example', 'secret', 'fake')), \
                patch.object(ev, 'evaluate_one', side_effect=classify), \
                patch.object(ev, 'write', side_effect=fragile_windows_write), patch('builtins.print'):
            out = Path(tmp)/'run'
            ev.run(out, 'dev', 'fake')
            self.assertEqual(len(visited), 42)
            self.assertEqual(len(aggregate_writes), 1)
            self.assertEqual(len(ev.read(out/'results.json')), 42)
            self.assertEqual(ev.read(out/'metrics.json')['correct'], 6)

    def test_failed_provider_attempt_is_saved_without_credential(self):
        secret = 'private-value-for-test-only'
        class Provider(OpenAICompatibleModel):
            def _complete(self, messages, tools):
                raise ValueError('failure contains '+secret)
        case = next(c for c in ev.read(ev.PANEL)['cases'] if c['split'] == 'dev')
        with tempfile.TemporaryDirectory() as tmp, patch.object(ev, 'model_from_env', return_value=Provider('https://invalid.example', secret, 'fake')):
            row = ev.evaluate_one(case, Path(tmp), 'fake', ev.Budget({'seconds': 60, 'requests': 2, 'tokens': 100000}))
            self.assertIsNone(row['response'])
            self.assertEqual(row['attempts'], 1)
            for path in Path(tmp).rglob('*.json'):
                self.assertNotIn(secret, path.read_text(encoding='utf-8'))
            self.assertEqual(ev.score([case], [row])['correct'], 0)


if __name__ == '__main__':
    unittest.main()
