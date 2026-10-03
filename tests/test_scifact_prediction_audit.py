import unittest
from copy import deepcopy

from evals.audit_scifact_predictions import score_predictions


class SciFactPredictionAuditTests(unittest.TestCase):
    def test_recomputes_macro_f1_from_original_claim_evidence(self):
        claims = [{'id': 1, 'evidence': {'10': [{'label': 'SUPPORT'}]}},
                  {'id': 2, 'evidence': {'20': [{'label': 'CONTRADICT'}]}},
                  {'id': 3, 'evidence': {}}]
        predictions = [{'id': 3, 'prediction': 'NOINFO'}, {'id': 1, 'prediction': 'SUPPORT'},
                       {'id': 2, 'prediction': 'SUPPORT'}]
        result = score_predictions(claims, predictions)
        self.assertEqual(result['metrics']['accuracy'], 2 / 3)
        self.assertAlmostEqual(result['metrics']['macro_f1'], 5 / 9)
        self.assertEqual(result['diagnostics']['confusion_matrix'], [[1, 0, 0], [1, 0, 0], [0, 0, 1]])
        for failure in ('missing', 'duplicate', 'foreign_id', 'fabricated_gold', 'invalid_label'):
            with self.subTest(failure=failure):
                rows = deepcopy(predictions)
                if failure == 'missing': rows.pop()
                if failure == 'duplicate': rows.append(deepcopy(rows[0]))
                if failure == 'foreign_id': rows[0]['id'] = 999
                if failure == 'fabricated_gold': rows[-1]['gold'] = 'SUPPORT'
                if failure == 'invalid_label': rows[0]['prediction'] = 'UNKNOWN'
                with self.assertRaises(ValueError): score_predictions(claims, rows)


if __name__ == '__main__':
    unittest.main()
