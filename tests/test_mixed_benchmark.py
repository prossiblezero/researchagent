import json
import unittest
import tempfile
from pathlib import Path

from research_agent.benchmark_datasets import DATASET_NAMES, load_contexts, load_tasks
from evals.run_mixed_benchmark import retrieval_metrics, seed, live_panel
from evals.summarize_product_benchmark import normalize_judgments

ROOT = Path(__file__).resolve().parents[1]


class MixedBenchmarkTests(unittest.TestCase):
    def test_fixed_counts_and_splits(self):
        expected = {"qasper": 80, "scifact": 60, "hotpotqa": 50, "longmemeval": 50, "project": 80}
        ids = set()
        for name in DATASET_NAMES:
            rows = load_tasks(name)
            self.assertEqual(len(rows), expected[name], name)
            for row in rows:
                self.assertNotIn(row["id"], ids)
                ids.add(row["id"])
                self.assertIn(row["split"], {"historical_regression", "engineering_regression"} if name == 'project' else {"dev", "held_out"})

    def test_gold_contexts_exist(self):
        for name in DATASET_NAMES:
            tasks = load_tasks(name)
            contexts = load_contexts(name, tasks)
            for row in tasks:
                self.assertTrue(set(row.get("gold_context_ids", [])) <= set(contexts) or row.get("workflow_only"), row["id"])

    def test_open_catalog_records_sources(self):
        catalog = json.loads((ROOT / "datasets" / "open" / "catalog.json").read_text(encoding="utf-8"))
        self.assertEqual(catalog["version"], "20260929-product-v2")
        self.assertEqual(sum(catalog[name]["tasks"] for name in ("qasper", "scifact", "hotpotqa", "longmemeval", "project")), 320)

    def test_scores_use_unique_delivered_parents_and_do_not_invent_refusal(self):
        row = retrieval_metrics({'gold_context_ids':['a','b']}, ['z','a','a','b'])
        self.assertEqual(row['recall_at5'],1)
        self.assertEqual(row['mrr_at5'],.5)
        absent = retrieval_metrics({'gold_context_ids':[], 'unanswerable':True}, [])
        self.assertIsNone(absent['recall_at5'])
        self.assertNotIn('abstained',absent)
        self.assertIsNone(retrieval_metrics({'gold_context_ids':['a'], 'retrieval_eligible':False},['a'])['recall_at5'])

    def test_real_sqlite_retrieval_never_receives_gold_and_maps_empty_chunks(self):
        contexts = {'a':{'id':'a','title':'First','text':'','source_id':'paper'},
                    'b':{'id':'b','title':'First','text':'The cobalt capacitor increases retrieval coverage by indexing original source paragraphs.','source_id':'paper'}}
        task = {'id':'host-only-id','benchmark':'qasper','source_id':'paper','query':'cobalt capacitor','gold_context_ids':['b']}
        with tempfile.TemporaryDirectory() as tmp:
            store, engine, info = seed(Path(tmp),'qasper',task,contexts,'lexical')
            result = engine.retrieve(info['space_id'],task['query'],top_k=5)
            self.assertEqual([info['context_map'][str(h['chunk_id'])] for h in result['results']],['b'])
            self.assertNotIn('host-only-id',json.dumps(engine._rows(info['space_id'],'documents')))
            self.assertEqual(next(iter(info['chunk_map'].values()))[1],1)

    def test_frozen_live_panel_and_exact_evidence_mapping(self):
        self.assertEqual(len(live_panel()),32)
        for t in load_tasks('qasper'):
            contexts=load_contexts('qasper')
            for answer in t['answers']:
                for cid in answer['gold_context_ids']:
                    self.assertIn(' '.join(contexts[cid]['text'].split()),[' '.join(e.split()) for e in answer['evidence']])
        seen=set()
        for t in load_tasks('longmemeval'):
            for c in t['contexts']:
                self.assertNotIn(c['id'],seen);seen.add(c['id'])
                self.assertTrue(c['messages'])

    def test_workflows_execute_named_product_tests(self):
        import sys
        sys.path.insert(0,str(ROOT/'tests'))
        for task in load_tasks('project'):
            if task.get('workflow_only'):
                suite=unittest.defaultTestLoader.loadTestsFromName(task['test_id'])
                self.assertEqual(suite.countTestCases(),1)
                self.assertNotIn('_FailedTest',str(suite))

    def test_judge_format_repair_preserves_failure_and_rejects_changed_answers(self):
        from evals.run_evidence_qa_acceptance import write, read
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);folder=root/'qa/case/baseline';folder.mkdir(parents=True)
            write(folder/'score.json',{'product_completed':True})
            write(folder/'judgment.json',{'valid':False,'error':'Invalid judge schema'})
            write(folder/'judge/requests/001.json',{
                'messages':[{'content':json.dumps({'answer':'Original answer'})}],
                'response':{'content':json.dumps({'correct':True,'citation_sufficient':True,'refused':False,'unsupported_additions':[]})}})
            (folder/'answer.md').write_text('Changed answer',encoding='utf-8')
            normalize_judgments(root)
            self.assertFalse(read(folder/'judgment.json')['valid'])
            (folder/'answer.md').write_text('Original answer',encoding='utf-8')
            normalize_judgments(root)
            self.assertTrue(read(folder/'judgment.json')['task_pass'])
            self.assertEqual(read(folder/'judge-schema-failure.json')['error'],'Invalid judge schema')


if __name__ == "__main__":
    unittest.main()
