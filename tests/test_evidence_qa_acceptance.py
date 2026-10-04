import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from evals import run_evidence_qa_acceptance as qa
from research_agent.contracts import ModelDecision
from research_agent.models import OpenAICompatibleModel
from research_agent.workbench_store import WorkbenchStore


class EvidenceQAAcceptanceTests(unittest.TestCase):
    def test_panel_validates_full_originals_and_category_balance(self):
        panel, corpus = qa.read(qa.PANEL), qa.read(qa.CORPUS)
        self.assertEqual(qa.validate_panel(panel, corpus), panel['counts'])
        self.assertEqual(len(panel['cases']), 16)
        changed = copy.deepcopy(panel)
        changed['cases'][0]['facts'][0]['evidence'][0]['quote'] += 'invented'
        with self.assertRaisesRegex(ValueError, 'Original'):
            qa.validate_panel(changed, corpus)

    def test_freeze_is_immutable_and_official_cannot_be_partial(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(qa, 'manifest', return_value={'qa.py': 'one'}):
            out = Path(tmp)
            condition = qa.freeze(out, 'sudocode-luna', ['normal-01'])
            self.assertEqual(qa.assert_frozen(out), condition)
            with self.assertRaisesRegex(ValueError, 'Frozen condition'):
                qa.freeze(out, 'other-model', ['normal-01'])
            with self.assertRaisesRegex(ValueError, 'Official'):
                qa.freeze(qa.OFFICIAL, 'sudocode-luna', ['normal-01'])
            with patch.object(qa, 'manifest', return_value={'qa.py': 'two'}):
                with self.assertRaisesRegex(ValueError, 'changed after freeze'):
                    qa.assert_frozen(out)

    def test_actual_request_export_is_complete_redacted_and_budgeted(self):
        class Fake(OpenAICompatibleModel):
            def _complete(self, messages, tools):
                self.last_usage = {'total_tokens': 11, 'input_tokens': 10, 'output_tokens': 1}
                return ModelDecision('final', content='answer')
        secret = 'private-key-which-must-not-leak'
        content = json.dumps({'passages': [{'content': '原文' * 10000 + 'TAIL'}]}, ensure_ascii=False)
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            model = qa.instrument(Fake('https://invalid.example', secret, 'fake'), folder,
                                  {**qa.BUDGET, 'max_model_attempts': 1})
            model.complete([{'role': 'user', 'content': content}, {'role': 'user', 'content': secret}], [])
            saved = qa.read(folder / 'requests/001.json')
            self.assertEqual(saved['messages'][0]['content'], content)
            self.assertIn('TAIL', saved['messages'][0]['content'])
            self.assertNotIn(secret, (folder / 'requests/001.json').read_text(encoding='utf8'))
            self.assertEqual(saved['usage']['total_tokens'], 11)
            with self.assertRaisesRegex(RuntimeError, 'budget exhausted'):
                model.complete([{'role': 'user', 'content': 'repeat'}], [])
            self.assertEqual(len(list((folder / 'requests').glob('*.json'))), 1)

    def test_keywords_are_never_final_scores_and_reviews_bind_answer(self):
        case = qa.read(qa.PANEL)['cases'][0]
        answer = 'EasyOCR 216，但这些数字是随便猜的。'
        self.assertTrue(all(qa.prefilter(case, answer).values()))
        row = {'answer_sha256': hashlib.sha256(answer.encode()).hexdigest(), 'product_completed': True}
        review = {'answer_sha256': row['answer_sha256'], 'reviewer': 'source auditor',
                  'reviewed_at': '2026-09-27', 'refused': False, 'refusal_correct': False,
                  'unsupported_additions': True, 'task_pass': False,
                  'facts': [{'id': f['id'], 'correct': False, 'supported': False,
                             'citation_sufficient': False} for f in case['facts']]}
        self.assertFalse(qa.reviewed_result(row, review, case))
        review['answer_sha256'] = 'changed'
        self.assertIsNone(qa.reviewed_result(row, review, case))

    def test_full_workbench_path_isolates_arms_and_never_replays_failure(self):
        class FailingProvider(OpenAICompatibleModel):
            calls = 0
            def _complete(self, messages, tools):
                type(self).calls += 1
                raise ValueError('Synthetic provider failure; no network')
        panel = qa.read(qa.PANEL)
        # The integration check exercises actual Workbench/SQLite but deliberately
        # substitutes index readiness and a non-network provider, not fake answers.
        ready = {'mode': 'hybrid', 'coverage': 1, 'indexed': 1, 'total': 1, 'error': ''}
        with tempfile.TemporaryDirectory() as tmp, patch.dict('os.environ', {'RETRIEVAL_MODE': 'lexical'}), \
             patch.object(qa.Retriever, 'status', return_value=ready), \
             patch.object(qa, 'model_from_env', side_effect=lambda _: FailingProvider('https://invalid.example', 'none', 'fake')):
            out = Path(tmp)
            qa.write(out / 'sources.json', qa.read(qa.CORPUS))
            info = qa.seed(out)
            seed_store = WorkbenchStore(out / 'seed.sqlite')
            self.assertTrue(Path(seed_store.space(info['space_id'])['download_root']).is_relative_to(out.resolve()))
            condition = {'model_id': 'sudocode-luna', 'budget': qa.BUDGET, 'arms': qa.ARMS}
            case = panel['cases'][0]
            one = qa.run_one(out, condition, info, case, 'baseline')
            two = qa.run_one(out, condition, info, case, 'candidate')
            self.assertEqual(one['status'], 'failed')
            self.assertEqual(two['status'], 'failed')
            calls = FailingProvider.calls
            self.assertEqual(qa.run_one(out, condition, info, case, 'baseline'), one)
            self.assertEqual(FailingProvider.calls, calls)
            self.assertEqual(WorkbenchStore(out / 'seed.sqlite').jobs(info['space_id']), [])
            for arm in qa.ARMS:
                store = WorkbenchStore(out / 'cases' / case['id'] / arm / 'public.sqlite')
                self.assertEqual(len(store.jobs(info['space_id'])), 1)
                self.assertEqual(len(store.conversations(info['space_id'])), 1)
            # If export was interrupted after a terminal database write, recovery
            # re-exports that terminal result without calling the provider again.
            folder = out / 'cases' / case['id'] / 'baseline'
            (folder / 'score.json').unlink()
            recovered = qa.run_one(out, condition, info, case, 'baseline')
            self.assertEqual(recovered['status'], 'failed')
            self.assertEqual(FailingProvider.calls, calls)


if __name__ == '__main__':
    unittest.main()
