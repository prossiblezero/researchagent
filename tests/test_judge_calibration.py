import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from evals import run_judge_calibration as ev
from research_agent.contracts import ModelDecision
from research_agent.models import OpenAICompatibleModel


def judgment(passed=True):
    return {'correct': passed, 'complete': True, 'citation_sufficient': True,
            'refused': False, 'unsupported_additions': False}


class JudgeCalibrationTests(unittest.TestCase):
    def test_panel_and_pass_contract(self):
        ev.validate(ev.read(ev.PANEL))
        self.assertTrue(ev.decision(judgment()))
        self.assertTrue(ev.decision({**judgment(), 'refused': True}))
        self.assertFalse(ev.decision({**judgment(), 'unsupported_additions': True}))
        self.assertFalse(ev.decision({**judgment(), 'complete': False}))
        with self.assertRaises(ValueError):
            ev.decision({**judgment(), 'correct': 'true'})

    def test_errors_and_missing_results_do_not_inflate_reliability(self):
        cases = [{'id': str(i), 'expected_pass': v, 'kind': 'probe', 'group': 'g'}
                 for i,v in enumerate([True, True, False, False, False])]
        rows = [{'id': '0', 'judgment': judgment()}, {'id': '1', 'judgment': judgment(False)},
                {'id': '2', 'judgment': judgment()}, {'id': '3', 'judgment': judgment(False), 'error': 'timeout'}]
        score = ev.score(cases, rows)
        self.assertEqual(score['correct'], 1)
        self.assertEqual(score['accuracy'], .2)
        self.assertEqual(score['errors_or_missing'], 2)
        self.assertEqual(score['false_accept_rate_valid_negatives'], 1)
        self.assertEqual(score['false_accept_worst_case_with_unknown'], 1)
        self.assertEqual(score['confusion']['negative_unknown'], 2)
        with self.assertRaises(ValueError):
            ev.score(cases, rows+[rows[0]])

    def test_quality_separates_delivery_raw_judge_and_source_review(self):
        rows = [{'id': '0', 'arm': 'a', 'product_completed': False,
                 'raw_judgment': {**judgment(), 'valid': True}},
                {'id': '1', 'arm': 'a', 'product_completed': True,
                 'raw_judgment': {'valid': False}, 'adjudication': {**judgment(), 'valid': True}},
                {'id': '2', 'arm': 'a', 'product_completed': True,
                 'raw_judgment': {**judgment(), 'complete': False, 'valid': True}}]
        result = ev.quality_metrics(rows)['a']
        self.assertEqual(result['raw']['confirmed_counts']['correct'], 1)
        self.assertEqual(result['raw']['unknown_delivered_counts']['correct'], 1)
        self.assertEqual(result['reviewed']['confirmed_counts']['correct'], 2)
        self.assertEqual(result['reviewed']['strict_passed'], 1)
        self.assertEqual(result['reviewed']['strict_pass_rate'], 1/3)
        with self.assertRaises(ValueError):
            ev.quality_metrics(rows+[rows[0]])

    def test_only_existing_judge_payload_is_sent_and_requests_saved(self):
        observed = []
        class Provider(OpenAICompatibleModel):
            def _complete(self, messages, tools):
                observed.append(messages)
                self.last_usage = {'total_tokens': 10, 'input_tokens': 8, 'output_tokens': 2}
                return ModelDecision('final', content=json.dumps(judgment()))
        case = copy.deepcopy(ev.read(ev.PANEL)['cases'][0])
        case['label_reason'] = 'PRIVATE_GOLD_SENTINEL'
        case['id'] = 'PRIVATE_ID_SENTINEL'
        with tempfile.TemporaryDirectory() as tmp, patch.object(ev, 'model_from_env', return_value=Provider('https://invalid.example','secret','fake')):
            row = ev.evaluate_one(case, 'frozen prompt', Path(tmp), ev.Budget(ev.LIMITS))
            self.assertIsNone(row['error'])
            self.assertEqual(row['attempts'], 1)
            sent = json.dumps(observed)
            self.assertNotIn('PRIVATE_GOLD_SENTINEL', sent)
            self.assertNotIn('PRIVATE_ID_SENTINEL', sent)
            self.assertNotIn('expected_pass', sent)
            self.assertEqual(json.loads(observed[0][1]['content'])['UNTRUSTED_DOCUMENT_DATA'], case['input'])
            self.assertEqual(ev.read(Path(tmp)/'cases'/case['id']/'requests/001.json')['tools'], [])
            with self.assertRaises(FileExistsError):
                ev.evaluate_one(case, 'frozen prompt', Path(tmp), ev.Budget(ev.LIMITS))

    def test_saved_quality_evidence_recomputes_without_changing_denominators(self):
        evidence = ev.read(ev.ROOT/'docs/research/llm-judge-results-20261013.json')['project_quality']
        self.assertEqual(len(evidence['rows']), 40)
        recomputed = ev.quality_metrics(evidence['rows'])
        self.assertEqual(recomputed, evidence['metrics'])
        self.assertEqual(recomputed['keyword']['reviewed']['strict_passed'], 13)
        self.assertEqual(recomputed['integrated']['reviewed']['strict_passed'], 15)
        self.assertEqual(recomputed['keyword']['raw']['unknown_delivered_counts']['correct'], 2)
        self.assertEqual(recomputed['integrated']['raw']['unknown_delivered_counts']['correct'], 1)

    def test_saved_calibration_recomputes_all_first_results(self):
        evidence = ev.read(ev.ROOT/'docs/research/llm-judge-results-20261013.json')['calibration']
        self.assertEqual(evidence['status'], 'completed')
        self.assertEqual(evidence['protocol']['panel_sha256'], ev.sha(ev.PANEL.read_bytes()))
        self.assertEqual(len(evidence['rows']), 80)
        computed = ev.score(ev.read(ev.PANEL)['cases'], evidence['rows'])
        for key, value in computed.items():
            self.assertEqual(value, evidence['metrics'][key], key)
        self.assertTrue(evidence['metrics']['source_unchanged'])
        self.assertEqual(computed['correct']+len(computed['failures']), 80)

    def test_model_failure_is_redacted_and_remains_an_error(self):
        secret = 'private-test-key'
        class Provider(OpenAICompatibleModel):
            def _complete(self, messages, tools):
                raise ValueError('error '+secret)
        case = ev.read(ev.PANEL)['cases'][0]
        with tempfile.TemporaryDirectory() as tmp, patch.object(ev, 'model_from_env', return_value=Provider('https://invalid.example',secret,'fake')):
            row = ev.evaluate_one(case, 'prompt', Path(tmp), ev.Budget(ev.LIMITS))
            self.assertIsNotNone(row['error'])
            self.assertEqual(ev.score([case], [row])['errors_or_missing'], 1)
            for path in Path(tmp).rglob('*.json'):
                self.assertNotIn(secret, path.read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
