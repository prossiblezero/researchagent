import itertools
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from evals.agent_metrics import MeteredModel, pass_at_k, pass_power_k, repeated_metrics, usage_summary
from evals.run_agent_benchmark import make_schedule, report, grade, context_trial, workbench_trial
from research_agent.contracts import ModelDecision

class BenchmarkChecks(unittest.TestCase):
    def test_estimators_against_exhaustive_subsets(self):
        for n in range(1,7):
            for c in range(n+1):
                population=[True]*c+[False]*(n-c)
                for k in range(1,n+1):
                    subsets=list(itertools.combinations(population,k))
                    self.assertAlmostEqual(pass_at_k(n,c,k),sum(any(x) for x in subsets)/len(subsets))
                    self.assertAlmostEqual(pass_power_k(n,c,k),sum(all(x) for x in subsets)/len(subsets))
        with self.assertRaises(ValueError):pass_at_k(2,1,3)

    def test_unjudged_does_not_silently_become_a_failure_or_success(self):
        result=repeated_metrics([{'case_id':'a','passed':True},{'case_id':'a','passed':None},{'case_id':'b','passed':False}])
        self.assertEqual(result['fully_judged_tasks'],1)
        self.assertEqual(result['unjudged_trials'],1)
        self.assertEqual(result['pass_at_k']['1']['estimate'],0)

    def test_usage_missing_and_error_are_visible(self):
        class Model:
            name='test';total_usage={}
            def complete(self,messages,tools):
                if messages[0]['content']=='known':
                    self.total_usage={'prompt_tokens':12,'completion_tokens':3,'total_tokens':15}
                    return ModelDecision('final',content='ok')
                raise RuntimeError('timeout')
        with tempfile.TemporaryDirectory() as folder:
            ledger=[];meter=MeteredModel(Model(),ledger,'route',Path(folder),'test')
            meter.complete([{'role':'user','content':'known'}],[])
            with self.assertRaises(RuntimeError):meter.complete([{'role':'user','content':'missing'}],[])
            stats=usage_summary(ledger)
            self.assertEqual(stats['known_token_subtotal']['total_tokens'],15)
            self.assertEqual(stats['unknown_usage_requests'],1)
            self.assertIsNone(stats['complete_token_total'])

    def test_schedule_no_missing_or_overlapping_trials(self):
        schedule=make_schedule([{'id':str(i)} for i in range(6)],3,2)
        self.assertEqual(len(schedule),72)
        self.assertEqual(len({s['id'] for s in schedule}),72)
        self.assertEqual(schedule,make_schedule([{'id':str(i)} for i in range(6)],3,2))

    def test_incomplete_judgment_rejected(self):
        case={'question':'test','criteria':['first','second'],'reference_evidence':[]}
        with patch('evals.run_agent_benchmark.model_json',return_value={'criteria':[{'id':0,'met':True,'reason':'ok'}],'grounded':True,'contradictions':[]}):
            with self.assertRaises(ValueError):grade(case,'answer',[],None)

    def test_empty_report_is_renderable(self):
        with tempfile.TemporaryDirectory() as folder:
            report(Path(folder),[],{'schedule':[],'model':'test','judge':'test','limitations':['test']})
            self.assertIn('<table>',(Path(folder)/'report.html').read_text(encoding='utf-8'))
            self.assertTrue(json.loads((Path(folder)/'metrics.json').read_text())['complete'])

    def test_context_track_runs_real_loop_with_fixed_model(self):
        from research_agent.models import OfflineModel
        from types import SimpleNamespace
        model=OfflineModel();model.total_usage={}
        spec={'id':'smoke','case_id':'smoke','condition':'source_stress','track':'context','source_chars':2048,'position':'head','repeat':1}
        with tempfile.TemporaryDirectory() as folder,patch('evals.run_agent_benchmark.model_from_env',return_value=model):
            row=context_trial(spec,SimpleNamespace(model='default'),Path(folder))
            self.assertTrue(row['passed'])
            self.assertTrue(row['anchor_reached_model'])
            self.assertGreaterEqual(row['model_requests'],3)
            self.assertEqual(row['network_requests'],0)

    def test_workbench_route_retrieval_answer_are_all_metered(self):
        from types import SimpleNamespace
        class Model:
            name='test-local'
            def __init__(self):self.total_usage={}
            def complete(self,messages,tools):
                self.total_usage={key:self.total_usage.get(key,0)+amount for key,amount in [('prompt_tokens',10),('completion_tokens',5),('total_tokens',15)]}
                prompt=messages[0]['content']
                if '意图路由器' in prompt:
                    result={'intent':'LOCAL_QA','reply':'','brief':'','assumptions':[],'effort':'none'}
                elif tools:
                    payload=next((json.loads(m['content'])['UNTRUSTED_TOOL_DATA'] for m in reversed(messages) if m['role']=='tool'),None)
                    if not payload:
                        return ModelDecision('tool_call',tool_name='retrieve',arguments={'query':'TableFormer','corpus':'documents'},call_id='retrieve')
                    if payload['kind']=='retrieve':
                        return ModelDecision('tool_call',tool_name='read_evidence',arguments={'ref_id':payload['results'][0]['ref_id']},call_id='read')
                    return ModelDecision('final',content='The report uses TableFormer. ['+payload['evidence_id']+']')
                elif '提取值得跨会话' in prompt:
                    result={'items':[]}
                else:
                    result={'criteria':[{'id':0,'met':True,'reason':'The statement matches the quoted source.'}],'grounded':True,'contradictions':[]}
                return ModelDecision('final',content=json.dumps(result))
        case={'id':'local-test','intent':'LOCAL_QA','question':'本地论文的模型名称？','criteria':['Name TableFormer.'],'reference_evidence':[],'local_titles':['Sample']}
        corpus=[{'title':'Sample','url':'https://example.org/paper','sha256':'fixed','metadata':{},'chunks':[{'content':'The sample report uses TableFormer to recover logical table structure.','page':1,'section':'Methods','line_start':1,'line_end':1}]}]
        spec={'id':'local-smoke','case_id':'local-test','track':'workbench','condition':'base','repeat':1}
        with tempfile.TemporaryDirectory() as folder,patch('evals.run_agent_benchmark.model_from_env',side_effect=lambda profile:Model()):
            args=SimpleNamespace(model='default',judge='default',download_root=Path(folder)/'downloads')
            row=workbench_trial(spec,case,args,corpus,Path(folder))
            self.assertTrue(row['passed'])
            self.assertEqual(row['model_requests'],5)
            self.assertEqual(row['usage']['complete_token_total']['total_tokens'],75)
            self.assertEqual([c['phase'] for c in row['calls']],['route','workbench','workbench','workbench','workbench','judge'])

if __name__=='__main__':unittest.main()
