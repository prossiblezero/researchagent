"""Split leakage and exact original-evidence checks for the new feedback panels."""
import copy
import json
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import patch

from research_agent import experiment_dataset as dataset
from research_agent.experiment_retrieval import frozen_data as historical_data


class FeedbackDevelopmentPanelTests(unittest.TestCase):
    def test_new_fact_groups_are_grounded_and_disjoint_from_historical_questions(self):
        historical, old_hash = historical_data()
        data, fingerprint = dataset.frozen_data()
        dev = [q for q in data['questions'] if q['split'] == 'dev']
        final = [q for q in data['questions'] if q['split'] == 'held_out']
        self.assertEqual(Counter(q['category'] for q in dev),
                         dict(direct=6, semantic=6, cross_document=6, unanswerable=6))
        self.assertEqual(Counter(q['category'] for q in final),
                         dict(direct=3, semantic=3, cross_document=3, unanswerable=3))
        self.assertEqual(data['dataset_info']['historical_questions'], 60)
        self.assertEqual(data['dataset_info']['historical_hash'], old_hash)
        self.assertEqual(len(fingerprint), 64)
        self.assertEqual({g['document'] for q in dev for g in q['gold']},
                         {'docling', 'react', 'pi', 'codex', 'openclaw', 'hermes'})
        self.assertEqual({g['document'] for q in final for g in q['gold']},
                         {'docling', 'react', 'pi', 'codex', 'openclaw', 'hermes'})
        self.assertFalse({g for q in dev for g in q['fact_groups']} &
                         {g for q in final for g in q['fact_groups']})
        self.assertEqual(len(historical['questions']), 60)

    def test_loader_rejects_fact_group_leakage_duplicate_text_and_false_evidence(self):
        source = json.loads(dataset.DEV_PANEL.read_text(encoding='utf-8'))
        final = json.loads(dataset.FINAL_PANEL.read_text(encoding='utf-8'))
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'panel.json'
            def rejected(rows):
                path.write_text(json.dumps(rows, ensure_ascii=False), encoding='utf-8')
                with patch.object(dataset, 'DEV_PANEL', path):
                    with self.assertRaises(ValueError):
                        dataset.frozen_data()
            leaked = copy.deepcopy(source)
            leaked[0]['fact_groups'] = final[0]['fact_groups']
            rejected(leaked)
            duplicate = copy.deepcopy(source)
            duplicate[0]['query'] = final[0]['query']
            rejected(duplicate)
            incorrect = copy.deepcopy(source)
            incorrect[0]['evidence'][0]['quote'] = 'fabricated evidence that is absent from frozen source'
            rejected(incorrect)

    def test_loader_verifies_original_source_reference_page_and_chunk_hash(self):
        source = json.loads(dataset.DEV_PANEL.read_text(encoding='utf-8'))
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'panel.json'
            for field, wrong in [('source_sha256', '0' * 64), ('chunk_sha256', '1' * 64), ('page', -1)]:
                with self.subTest(field=field):
                    changed = copy.deepcopy(source)
                    changed[0]['evidence'][0][field] = wrong
                    path.write_text(json.dumps(changed, ensure_ascii=False), encoding='utf-8')
                    with patch.object(dataset, 'DEV_PANEL', path):
                        with self.assertRaisesRegex(ValueError, '标注依据与冻结原文不一致'):
                            dataset.frozen_data()

    def test_panel_change_changes_fingerprint_and_authoring_metadata_is_versioned(self):
        data, before = dataset.frozen_data()
        changed = json.loads(dataset.DEV_PANEL.read_text(encoding='utf-8'))
        changed[0]['annotation_note'] += ' clarification'
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'panel.json'
            path.write_text(json.dumps(changed, ensure_ascii=False), encoding='utf-8')
            with patch.object(dataset, 'DEV_PANEL', path):
                _, after = dataset.frozen_data()
        self.assertNotEqual(before, after)
        self.assertEqual(data['dataset_info']['version'], dataset.VERSION)


if __name__ == '__main__':
    unittest.main()
