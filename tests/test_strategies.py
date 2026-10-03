import json
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock

from research_agent.workbench_store import WorkbenchStore, Conflict
from research_agent.strategies import Strategies, StrategyRetriever, BASELINE, digest
from research_agent.contracts import ModelDecision
from research_agent.library import Library
from research_agent.retrieval import Retriever
from research_agent.loop import _tool_messages


class StrategyTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=WorkbenchStore(Path(self.temp.name)/'state.db')
        self.sid=self.store.save_space({'name':'strategies'})['id']
        self.chat=self.store.create_conversation(self.sid,'independent')['id']
        self.p=Strategies(self.store)
        self.store.message(self.sid,self.chat,'user','question')
        self.job=self.store.enqueue(self.sid,self.chat,'question','brief',[])
        self.store.claim_next();self.job=self.store.job(self.sid,self.job['id'])
        self.base=self.p.pin(self.job)
        feedback=self.p.feedback(self.sid,self.job['id'],'retrieval_miss','Missing primary source')
        self.version=self.p.propose(self.sid,feedback['id'])

    def evaluation(self,good=True,missing=False):
        manifest=dict(dataset_hash='fixed',source_hash='public',model='',budget_hash='8tools',scorer='v1',split='validation')
        manifest['model']='model'
        pairs=[{'id':str(i),'baseline':{'success':i>1,'citation_safe':True,'seconds':10,'tokens':100,'condition_hash':digest(manifest)},
                'candidate':{'success':good,'citation_safe':True,'seconds':12,'tokens':None if missing else 120,'condition_hash':digest(manifest)}} for i in range(6)]
        return self.p.evaluate(self.sid,self.version['id'],self.base['id'],manifest,pairs)

    def test_pin_activation_rollback_and_failed_gate(self):
        failed=self.evaluation(False)
        with self.assertRaises(Conflict):self.p.activate(self.sid,self.version['id'],failed['id'])
        self.assertFalse(self.evaluation(missing=True)['passed'])
        good=self.evaluation();self.assertTrue(good['passed'])
        self.p.activate(self.sid,self.version['id'],good['id'])
        self.assertEqual(self.p.pin(self.job),self.base)
        self.assertEqual(self.p.rollback(self.sid)['active_id'],self.base['id'])
        self.assertEqual(len(self.p.overview(self.sid)['evaluations']),3)

    def test_scope_and_cancelled_late_write(self):
        other=self.store.save_space({'name':'other'})['id']
        with self.assertRaises(ValueError):self.p.propose(other,self.version['feedback_id'])
        with closing(self.store._connect()) as db,db:db.execute("UPDATE research_jobs SET status='cancelled' WHERE id=?",(self.job['id'],))
        self.p.finish(self.job,{'success':False})
        self.assertIsNone(self.p.overview(self.sid)['runs'][0]['outcome'])

    def test_weak_feedback_does_not_enable_arbitrary_policy(self):
        f=self.p.feedback(self.sid,self.job['id'],'positive','good')
        with self.assertRaises(ValueError):self.p.propose(self.sid,f['id'])
        self.assertEqual(self.p.overview(self.sid)['active_id'],self.base['id'])

    def test_planning_failure_falls_back_without_changing_scope(self):
        base=Mock();base.retrieve.return_value={'results':[]}
        model=Mock();model.complete.return_value=ModelDecision('final',content='{"queries":["wrong", 3]}')
        adapter=StrategyRetriever(base,self.version,model)
        result=adapter.retrieve(self.sid,'question',scope='current',artifact_ids=['paper'])
        self.assertTrue(result['strategy']['planning_error'])
        self.assertEqual(base.retrieve.call_args.kwargs['query_variants'],[])
        self.assertEqual(base.retrieve.call_args.kwargs['artifact_ids'],['paper'])
        adapter.retrieve(self.sid,'question',scope='current')
        self.assertEqual(model.complete.call_count,1)
        adapter.retrieve(self.sid,'old memory',corpus='history')
        self.assertEqual(model.complete.call_count,1)

    def test_monitor_rolls_back_after_five_bad_runs(self):
        e=self.evaluation();self.p.activate(self.sid,self.version['id'],e['id'])
        for i in range(5):
            self.store.message(self.sid,self.chat,'user','next')
            job=self.store.enqueue(self.sid,self.chat,'next','brief',[],model_name='model')
            with closing(self.store._connect()) as db,db:db.execute("UPDATE research_jobs SET status='running' WHERE id=?",(job['id'],))
            job=self.store.job(self.sid,job['id']);self.p.pin(job);self.p.finish(job,{'success':False})
        self.assertEqual(self.p.overview(self.sid)['active_id'],self.base['id'])
        self.assertEqual(self.p.overview(self.sid)['events'][0]['kind'],'auto_rollback')

    def test_unvalidated_model_and_resume_generation(self):
        e=self.evaluation();self.p.activate(self.sid,self.version['id'],e['id'])
        self.store.message(self.sid,self.chat,'user','new model')
        job=self.store.enqueue(self.sid,self.chat,'new model','brief',[],model_name='unvalidated')
        pin=self.p.pin(job);self.assertEqual(pin['id'],self.base['id'])
        self.assertEqual(pin['trigger'],'model_not_evaluated_use_baseline')
        with closing(self.store._connect()) as db,db:
            db.execute("UPDATE research_jobs SET status='running',execution_generation=1 WHERE id=?",(job['id'],))
        self.p.finish(job,{'success':False})
        self.assertFalse([r for r in self.p.overview(self.sid)['attempts'] if r['job_id']==job['id']])
        current=self.store.job(self.sid,job['id']);self.p.finish(current,{'success':True})
        self.assertEqual(self.p.pin(current),pin)

    def test_query_variants_cannot_expand_document_authorization(self):
        lib=Library(self.store)
        def add(sid,key,text):
            return lib.save(sid,{'kind':'document','title':key,'url':'','canonical_id':key,'metadata':{},'data':None,'warnings':[],
                'boundary':'test text','chunks':[{'ordinal':0,'section':'','text':text+' Original source passage for authorization test.','page':1,'line_start':1,'line_end':1}]})[0]
        allowed=add(self.sid,'allowed','alpha target evidence')
        add(self.sid,'filtered','beta restricted by artifact selection')
        other=self.store.save_space({'name':'unrelated'})['id'];add(other,'secret','beta unrelated workspace')
        retriever=Retriever(self.store,lib)
        response=retriever.retrieve(self.sid,'alpha',artifact_ids=[allowed['id']],query_variants=['beta'],coverage_rerank=True)
        self.assertTrue(response['results'])
        self.assertTrue(all(r['owner_id']==allowed['id'] and r['space_id']==self.sid for r in response['results']))

    def test_attribution_never_changes_model_tool_context(self):
        data={'ok':True,'results':[{'ref_id':'D1-0','snippet':'original'}]}
        decision=ModelDecision('tool_call',tool_name='retrieve',arguments={'query':'source'})
        original=_tool_messages(decision,'call',decision.arguments,data)
        traced={**data,'strategy':{'version_id':'new','queries':['source','alternative']}}
        self.assertEqual(_tool_messages(decision,'call',decision.arguments,traced),original)
        self.assertIn('strategy',traced)

    def test_evaluation_refusal_does_not_need_an_invented_citation(self):
        from evals.run_harness_strategies import score_answer
        from types import SimpleNamespace
        result=SimpleNamespace(answer='无法核实，也不会猜测。',status='ok',invalid_citations=[],
            claims=[SimpleNamespace(status='INSUFFICIENT')])
        case={'patterns':['无法核实'],'unanswerable':True}
        self.assertTrue(score_answer(result,case,False)[2])
        self.assertFalse(score_answer(result,{'patterns':['无法核实']},False)[2])
        result.invalid_citations=['E999'];self.assertFalse(score_answer(result,case,False)[2])


if __name__=='__main__':unittest.main()
