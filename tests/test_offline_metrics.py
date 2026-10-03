"""Offline security scoring follows actual tool denial, not a terminal label."""
import json
import unittest
from dataclasses import replace
from evals.run_metrics import CASES_FILE, run_case, score_case


class OfflineMetricsTests(unittest.TestCase):
    def test_denial_requires_trace_and_zero_executed_tools(self):
        case = next(c for c in json.loads(CASES_FILE.read_text(encoding='utf-8'))['cases']
                    if c['id'] == 'policy_denial')
        result, events = run_case(case)
        self.assertEqual(result.tool_calls, 0)
        self.assertEqual(score_case(case, result, events)[1], [])
        self.assertIn('unauthorized_read_denied', score_case(case, result, [])[1])
        self.assertIn('unauthorized_read_denied', score_case(case, replace(result, tool_calls=1), events)[1])
