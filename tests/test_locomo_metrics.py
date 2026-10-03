"""Independent answer/evidence scoring and frozen host-only label boundaries."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from research_agent.coding_tool import validate_metric_contract
from research_agent.locomo_metrics import answer_scores, audit_locomo_result, score_predictions


class LoCoMoMetricsTests(unittest.TestCase):
    def setUp(self):
        self.tasks = [{'id': 'q1', 'conversation_id': 'c1', 'split': 'development', 'category': 1},
                      {'id': 'q2', 'conversation_id': 'c1', 'split': 'development', 'category': 2}]
        self.corpus = [{'conversation_id': 'c1', 'split': 'development', 'conversation': {
            'session_1': [{'dia_id': 'D1:1'}, {'dia_id': 'D1:2'}, {'dia_id': 'D1:3'}]}}]
        self.labels = [{'id': 'q1', 'answer': 'Oliver, Luna', 'gold_evidence': ['D1:1', 'D1:2'], 'retrieval_scorable': True},
                       {'id': 'q2', 'answer': 2023, 'gold_evidence': [], 'retrieval_scorable': False}]
        self.rows = [{'task_id': 'q1', 'seed': 13, 'answer': 'Luna', 'evidence_ids': ['D1:3', 'D1:1']},
                     {'task_id': 'q2', 'seed': 13, 'answer': '2023', 'evidence_ids': []}]

    def test_matches_upstream_set_f1_and_counts_unmapped_questions_for_answers(self):
        self.assertEqual(answer_scores('Luna Luna!', 'Oliver, Luna')['answer_f1'], 2/3)
        self.assertEqual(answer_scores('', '')['exact_match'], 0)
        result = score_predictions(self.tasks, self.corpus, self.labels, self.rows, [13])
        self.assertAlmostEqual(result['metrics']['answer_f1'], 5/6)
        self.assertEqual(result['metrics']['exact_match'], .5)
        self.assertEqual(result['metrics']['evidence_recall_at_5'], .5)
        self.assertEqual((result['answer_task_count'], result['evidence_task_count']), (2, 1))
        self.assertEqual(result['per_category'][1]['metrics']['answer_f1'], 1)

    def test_rejects_missing_duplicate_wrong_seed_and_out_of_conversation_predictions(self):
        for rows in (self.rows[:1], [self.rows[0], self.rows[0]],
                     [{**self.rows[0], 'seed': True}, self.rows[1]],
                     [{**self.rows[0], 'evidence_ids': ['other:D1:1']}, self.rows[1]],
                     [{**self.rows[0], 'evidence_ids': ['D1:1', 'D1:1']}, self.rows[1]]):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                score_predictions(self.tasks, self.corpus, self.labels, rows, [13])
        with self.assertRaises(ValueError):
            score_predictions(self.tasks, self.corpus, self.labels, self.rows, list(range(65)))

    def test_host_ignores_self_reported_score_and_rejects_modified_labels_or_split(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); workspace = root/'work'; workspace.mkdir()
            labels = root/'scoring-labels.json'
            labels.write_text(json.dumps(self.labels), encoding='utf-8')
            for name, data in [('tasks', self.tasks), ('corpus', self.corpus)]:
                (workspace/(name+'.json')).write_text(json.dumps(data), encoding='utf-8')
            raw = '\n'.join(json.dumps(row) for row in self.rows)
            (workspace/'predictions.jsonl').write_text(raw, encoding='utf-8')
            contract = {'name': 'locomo_qa_v1', 'seeds': [13], 'tasks_path': 'tasks.json', 'corpus_path': 'corpus.json',
                        'labels_sha256': hashlib.sha256(labels.read_bytes()).hexdigest(),
                        **{name+'_sha256': hashlib.sha256((workspace/(name+'.json')).read_bytes()).hexdigest()
                           for name in ('tasks', 'corpus')}}
            self.assertEqual(validate_metric_contract(contract), contract)
            with self.assertRaises(ValueError):
                validate_metric_contract({**contract, 'labels_path': str(labels)})
            result = {'metrics': {'answer_f1': 1.0, 'latency_ms': 0}, 'diagnostics': {'predictions_path': 'predictions.jsonl'},
                      'config': {'dataset': 'locomo', 'dataset_version': contract['tasks_sha256'],
                                 'split': 'development', 'seeds': [13]}}
            scored, audit = audit_locomo_result(workspace, result, contract, labels)
            self.assertAlmostEqual(scored['metrics']['answer_f1'], 5/6)
            self.assertNotIn('latency_ms', scored['metrics'])
            self.assertEqual(audit['reported_metrics']['answer_f1'], 1)
            self.assertEqual(audit['predictions_sha256'], hashlib.sha256((workspace/'predictions.jsonl').read_bytes()).hexdigest())
            for field, value in [('split', 'holdout'), ('split', []), ('split', {}), ('dataset_version', 'different')]:
                wrong = copy.deepcopy(result); wrong['config'][field] = value
                with self.subTest(field=field), self.assertRaises(ValueError):
                    audit_locomo_result(workspace, wrong, contract, labels)
            wrong = copy.deepcopy(result); wrong['config']['seeds'] = [999]
            changed = [{**row, 'seed': 999} for row in self.rows]
            (workspace/'predictions.jsonl').write_text('\n'.join(json.dumps(row) for row in changed), encoding='utf-8')
            with self.assertRaises(ValueError):
                audit_locomo_result(workspace, wrong, contract, labels)
            labels.write_text('[]', encoding='utf-8')
            with self.assertRaises(ValueError):
                audit_locomo_result(workspace, result, contract, labels)
