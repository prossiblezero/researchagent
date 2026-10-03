from pathlib import Path
import hashlib, json, unittest

ROOT = Path(__file__).resolve().parents[1]


class DatasetCatalogTests(unittest.TestCase):
    def setUp(self):
        self.catalog_path = ROOT / 'datasets' / 'manifest.json'
        self.catalog = json.loads(self.catalog_path.read_text(encoding='utf-8'))

    def test_catalog_entries_are_canonical_and_hashes_match(self):
        entries = self.catalog['datasets']
        paths = [entry['path'] for entry in entries]
        self.assertEqual(len(paths), len(set(paths)))
        for entry in entries:
            path = ROOT / entry['path']
            self.assertTrue(path.is_file(), entry['path'])
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), entry['sha256'], entry['path'])
            self.assertFalse(entry['path'].startswith('evals/'))

    def test_all_active_dataset_inputs_are_cataloged(self):
        actual = {
            path.relative_to(ROOT).as_posix()
            for path in (ROOT / 'datasets').rglob('*')
            if path.is_file() and path.suffix in {'.json', '.md'}
            and path.name not in {'README.md', 'DATA_LICENSE.md', 'manifest.json'}
        }
        cataloged = {entry['path'] for entry in self.catalog['datasets']}
        self.assertEqual(actual, cataloged)

    def test_open_benchmark_references_have_explicit_execution_status(self):
        refs = self.catalog['open_source_benchmark_registry']
        self.assertGreaterEqual(len(refs), 1)
        allowed = {'not_run', 'fixed_subset_run'}
        self.assertTrue(all(ref['execution_status'] in allowed for ref in refs))
        self.assertTrue(any(ref['execution_status'] == 'fixed_subset_run' for ref in refs))

    def test_active_test_modules_are_under_tests(self):
        outside = []
        for path in ROOT.rglob('test_*.py'):
            if any(part.startswith('.venv') or part in {'.git', 'venv', 'experiments', 'reports', 'archives'} for part in path.parts):
                continue
            if 'tests' not in path.relative_to(ROOT).parts:
                outside.append(path.relative_to(ROOT).as_posix())
        self.assertEqual(outside, [])

    def test_locomo_holdout_is_separate_and_prediction_inputs_have_no_labels(self):
        root = ROOT / 'datasets/open/locomo'
        load = lambda name: json.loads((root / name).read_text(encoding='utf-8'))
        tasks, labels, corpus = load('tasks.json'), load('labels.json'), load('corpus.json')
        self.assertEqual(len(tasks), 80)
        self.assertEqual({t['id'] for t in tasks}, {t['id'] for t in labels})
        development = {t['conversation_id'] for t in tasks if t['split'] == 'development'}
        holdout = {t['conversation_id'] for t in tasks if t['split'] == 'holdout'}
        self.assertFalse(development & holdout)
        self.assertEqual((len(development), len(holdout)), (2, 2))
        for task in tasks:
            self.assertFalse({'answer', 'gold_evidence', 'evidence'} & task.keys())
            self.assertIn(task['category'], (1, 2, 3, 4))
        turns = {}
        for item in corpus:
            self.assertNotIn('qa', item)
            turns[item['conversation_id']] = {t['dia_id'] for k, v in item['conversation'].items()
                if k.startswith('session_') and isinstance(v, list) for t in v}
        task_by_id = {task['id']: task for task in tasks}
        for label in labels:
            known = turns[task_by_id[label['id']]['conversation_id']]
            self.assertEqual(label['unmapped_evidence'], sorted(set(label['gold_evidence']) - known))
            self.assertEqual(label['retrieval_scorable'], bool(label['gold_evidence']) and not label['unmapped_evidence'])


if __name__ == '__main__':
    unittest.main()
