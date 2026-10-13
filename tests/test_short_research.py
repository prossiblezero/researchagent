"""Acceptance properties of the short-loop scorer and shared resource accounting."""
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

from evals.short_research_task import score
from evals.run_short_research import Budget, independent_score, stage, write as safe_write
from evals.prepare_short_research import write
from research_agent.experiment_process import ProcessNotStarted


class ShortResearchTests(unittest.TestCase):
    def test_scores_require_complete_valid_predictions(self):
        cases = [{'id': 'a', 'documents': [{'id': 'x'}, {'id': 'y'}, {'id': 'z'}], 'gold': ['x', 'z']}]
        self.assertEqual(score(cases, {'a': ['x', 'y']}, 2)['recall_at_k'], 0.5)
        for invalid in ({}, {'a': []}, {'a': ['x', 'x']}, {'a': ['x', 'outside']}, {'a': ['x', 'y'], 'extra': []}):
            with self.assertRaises(ValueError):
                score(cases, invalid, 2)
        with self.assertRaises(ValueError):
            score([], {}, 2)

    def test_controller_coder_cached_and_failed_usage_share_budget(self):
        budget = Budget({'wall_seconds': 360, 'total_reported_tokens': 100}, Path('.'))
        budget.models = [types.SimpleNamespace(usage_records=[{'input_tokens': 30, 'output_tokens': 10, 'total_tokens': 40}])]
        budget.cli = [{'finished': True, 'usage': [{'input_tokens': 50, 'cached_input_tokens': 30, 'output_tokens': 11}]}]
        self.assertEqual(budget.tokens, 101)  # Cached input already belongs to input_tokens.
        self.assertFalse(budget.summary()['token_budget_valid'])
        with self.assertRaises(RuntimeError):
            budget.guard()
        budget.cli = [{'finished': True, 'usage': []}]
        self.assertTrue(budget.unknown)
        self.assertFalse(budget.summary()['token_budget_valid'])
        budget.cli = []
        budget.models[0].usage_records = [{'input_tokens': 120, 'output_tokens': 10, 'total_tokens': 10}]
        self.assertEqual(budget.tokens, 130)
        self.assertFalse(budget.summary()['token_budget_valid'])

    def test_no_submission_or_modified_evaluator_cannot_be_scored(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write(root/'packets/toy/dev.json', {'cases': []})
            protocol = {'wall_seconds': 360, 'total_reported_tokens': 400000}
            workspace = root/'workspace'
            stage(workspace, root/'packets', 'toy', protocol)
            with patch('evals.run_short_research.native.run_process', side_effect=AssertionError('Must not execute')):
                missing = independent_score(root/'result1', workspace, root/'packets', 'toy', protocol)
                self.assertFalse(missing['valid_submission'])
                (workspace/'evaluate.py').write_text('raise RuntimeError("tampered")')
                tampered = independent_score(root/'result2', workspace, root/'packets', 'toy', protocol)
                self.assertIn('Protected input modified', tampered['error'])

    def test_scoring_freezes_code_and_uses_separate_split_workspaces(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            packet = {'task': 'toy', 'version': 'v1', 'split': 'dev', 'k': 1,
                      'cases': [{'id': 'q', 'query': 'apple', 'documents': [{'id': 'x', 'text': 'apple'}], 'gold': ['x']}]}
            for split in ('dev', 'held_out'):
                write(root/'packets/toy'/(split+'.json'), {**packet, 'split': split})
            protocol = {'wall_seconds': 360, 'total_reported_tokens': 400000}
            ws = root/'workspace'
            stage(ws, root/'packets', 'toy', protocol)
            write(ws/'submission.json', {'chosen_method': 'method', 'dev_recall': 1.0})
            observed = []
            def command(workspace, args, private, *, readonly_paths):
                self.assertIn(workspace/'method.py', readonly_paths)
                self.assertIn(root/'packets', private)
                return args
            def execute(args, workspace, **kwargs):
                observed.append(workspace)
                data = json.loads((workspace/args[-3]).read_text(encoding='utf-8'))
                self.assertNotIn('gold', data['cases'][0])
                write(workspace/args[-1], {'predictions': {'q': ['x']}})
                return {'exit_code': 0, 'termination': 'completed'}
            with patch('evals.run_short_research.ROOT', root), patch('evals.run_short_research.native.sandbox_command', command), patch('evals.run_short_research.native.run_process', execute):
                result = independent_score(root/'out', ws, root/'packets', 'toy', protocol)
            self.assertTrue(result['valid_submission'], result)
            self.assertEqual(len(set(observed)), 2)

    def test_receipt_persistence_redacts_exact_secret(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict('os.environ', {'SUDOCODE_API_KEY': 'synthetic-key-123456789'}):
            path = Path(tmp)/'receipt.json'
            safe_write(path, {'stderr': 'failed synthetic-key-123456789'})
            self.assertNotIn('synthetic-key-123456789', path.read_text(encoding='utf-8'))

    def test_proven_prelaunch_failure_has_zero_provider_usage(self):
        with tempfile.TemporaryDirectory() as tmp:
            budget = Budget({'wall_seconds': 360, 'total_reported_tokens': 400000}, Path(tmp))
            with patch('evals.run_short_research.native.run_process', side_effect=ProcessNotStarted('preflight')):
                with self.assertRaises(ProcessNotStarted):
                    budget.process(['codex', 'exec'], Path(tmp), timeout=10)
            self.assertFalse(budget.unknown)
            self.assertEqual(budget.tokens, 0)
            self.assertTrue((Path(tmp)/'usage.json').is_file())


if __name__ == '__main__':
    unittest.main()
