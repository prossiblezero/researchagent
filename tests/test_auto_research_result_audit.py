import unittest
import json
import tempfile
from pathlib import Path

from evals.audit_auto_research_results import audit, score_rankings


class RankingAuditTests(unittest.TestCase):
    def test_recall_is_fraction_of_gold_and_every_seed_task_is_required(self):
        tasks = [{'id': 'q', 'source_id': 'p', 'gold_context_ids': ['a', 'b']}]
        corpus = [{'id': i, 'source_id': 'p'} for i in 'ab']
        rows = [{'seed': seed, 'task_id': 'q', 'rankings': [{'paragraph_id': 'a'}]} for seed in (13, 17)]
        result = score_rankings(tasks, corpus, rows, [13, 17])
        self.assertEqual(result['metrics'], {'recall_at_5': .5, 'mrr_at_5': 1, 'hit_at_5': 1})
        self.assertTrue(result['identical_top5_across_seeds'])
        for invalid in (rows[:1], rows + rows[:1], rows + [{'seed': 29, **rows[0]}]):
            with self.assertRaises(ValueError):
                score_rankings(tasks, corpus, invalid, [13, 17])
        with self.assertRaises(ValueError):
            score_rankings(tasks, corpus, [{'seed': 13, 'task_id': 'q', 'rankings': [{'paragraph_id': 'unknown'}]}], [13])

    def test_jsonl_audit_requires_all_contract_metrics(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            workspace = root / 'workspace'
            (workspace / 'data/qasper').mkdir(parents=True)
            (workspace / 'results').mkdir()
            tasks = [{'id': 'q', 'source_id': 'p', 'gold_context_ids': ['a', 'b']}]
            corpus = [{'id': 'a', 'source_id': 'p'}, {'id': 'b', 'source_id': 'p'}]
            (workspace / 'data/qasper/tasks.json').write_text(json.dumps({'tasks': tasks}), encoding='utf-8')
            (workspace / 'data/qasper/corpus.json').write_text(json.dumps(corpus), encoding='utf-8')
            ranking = workspace / 'results/rankings.jsonl'
            ranking.write_text(json.dumps({'seed': 13, 'task_id': 'q', 'rankings': [{'paragraph_id': 'a'}]}) + '\n', encoding='utf-8')
            state = {'id': 'task-1', 'request': {'plan': json.dumps({'version': 1})},
                     'state': {'measurements': [{'name': 'candidate', 'role': 'candidate', 'valid': True,
                         'script_sha256': 'script', 'result': {'config': {'dataset': 'qasper', 'seeds': [13]},
                         'diagnostics': {'ranking_path': 'results\\rankings.jsonl'}}, 'metrics': {'foo': 1.0}}]}}
            run = root / 'run'
            run.mkdir()
            (run / 'inputs.json').write_text(json.dumps({'workspace': str(workspace), 'inputs': [
                {'destination': 'data/qasper/tasks.json', 'sha256': __import__('hashlib').sha256((workspace / 'data/qasper/tasks.json').read_bytes()).hexdigest()},
                {'destination': 'data/qasper/corpus.json', 'sha256': __import__('hashlib').sha256((workspace / 'data/qasper/corpus.json').read_bytes()).hexdigest()}]}), encoding='utf-8')
            (run / 'coding-tasks.json').write_text(json.dumps([state]), encoding='utf-8')
            result = audit(run, root / 'audit', 'qasper')
            self.assertFalse(result['scores_reproduced'])
            self.assertIn('recall_at_5', result['measurements'][0]['differences'])


class ContinuationAuditTests(unittest.TestCase):
    def setUp(self):
        import hashlib
        from tests.test_locomo_metrics import LoCoMoMetricsTests
        from research_agent.locomo_metrics import score_predictions
        fixture = LoCoMoMetricsTests()
        fixture.setUp()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.run = self.root / 'run'
        self.workspace = self.root / 'workspace'
        self.sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
        self.write(self.workspace / 'data/locomo/tasks.json', fixture.tasks)
        self.write(self.workspace / 'data/locomo/corpus.json', fixture.corpus)
        self.write(self.workspace / 'results/frozen.json', {'turn': 2})
        self.write(self.workspace / 'results/resume.json', {'turn': 1})
        self.write(self.run / 'auto-research/job/scoring-labels.json', fixture.labels)
        self.write(self.run / 'job.json', {'id': 'job'})
        names = ['data/locomo/tasks.json', 'data/locomo/corpus.json', 'results/frozen.json', 'results/resume.json']
        self.inputs = {'workspace': str(self.workspace), 'inputs': [
            {'destination': name, 'sha256': self.sha(self.workspace / name)} for name in names]}
        self.write(self.run / 'inputs.json', self.inputs)
        self.write(self.run / 'cases.json', {'campaign': {'seeds': [13],
            'labels_sha256': self.sha(self.run / 'auto-research/job/scoring-labels.json'),
            'frozen_sources': {'results/frozen.json': self.sha(self.workspace / 'results/frozen.json')}}})
        prediction = self.run / 'coding-tools/task/predictions.jsonl'
        prediction.parent.mkdir(parents=True)
        prediction.write_text('\n'.join(json.dumps(row) for row in fixture.rows) + '\n', encoding='utf-8')
        scored = score_predictions(fixture.tasks, fixture.corpus, fixture.labels, fixture.rows, [13])
        self.write(self.run / 'coding-tasks.json', [{'id': 'task', 'request': {'plan': json.dumps({'version': 1})},
            'state': {'measurements': [{'name': 'candidate', 'role': 'candidate', 'valid': True,
                'script_sha256': 'unchanged-script', 'metrics': scored['metrics'],
                'result': {'config': {'dataset': 'locomo', 'dataset_version': self.sha(self.workspace / 'data/locomo/tasks.json'),
                                     'split': 'development', 'seeds': [13]}},
                'prediction_artifacts': {'predictions_path': {'path': 'predictions.jsonl', 'sha256': self.sha(prediction)}}}]}}])
        # This is an explicitly writable continuation checkpoint, not a score input.
        self.write(self.workspace / 'results/resume.json', {'turn': 3})

    @staticmethod
    def write(path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding='utf-8')

    def test_mutable_continuation_checkpoint_does_not_prevent_saved_score_audit(self):
        result = audit(self.run, self.root / 'audit', 'locomo')
        self.assertTrue(result['scores_reproduced'])
        self.assertEqual(len(result['measurements']), 1)
        self.assertEqual(result['measurements'][0]['answer_task_count'], 2)
        self.assertEqual(result['measurements'][0]['evidence_task_count'], 1)
        self.assertEqual(result['measurements'][0]['prediction_origin'], 'measurement_snapshot')

    def test_score_inputs_frozen_sources_and_manifest_coverage_still_checked(self):
        # Start with a valid resumed run, then prove each real frozen boundary rejects changes.
        self.assertTrue(audit(self.run, self.root / 'initial', 'locomo')['scores_reproduced'])
        paths = [self.workspace / 'data/locomo/tasks.json', self.workspace / 'data/locomo/corpus.json',
                 self.workspace / 'results/frozen.json', self.run / 'auto-research/job/scoring-labels.json',
                 self.run / 'coding-tools/task/predictions.jsonl']
        for index, path in enumerate(paths):
            original = path.read_bytes()
            path.write_bytes(original + b' ')
            with self.subTest(path=path.name), self.assertRaises(ValueError):
                audit(self.run, self.root / ('changed-' + str(index)), 'locomo')
            path.write_bytes(original)
        for index, items in enumerate([self.inputs['inputs'][1:], self.inputs['inputs'] + self.inputs['inputs'][:1]]):
            self.write(self.run / 'inputs.json', {**self.inputs, 'inputs': items})
            with self.subTest(manifest=index), self.assertRaises(ValueError):
                audit(self.run, self.root / ('manifest-' + str(index)), 'locomo')
