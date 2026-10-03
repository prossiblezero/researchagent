"""Online original-evidence selection: bounds, authorization and paid-call recovery."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from contextlib import closing

from research_agent.contracts import ModelDecision
from research_agent.library import Library
from research_agent.loop import ResearchAgent
from research_agent.retrieval import Retriever
from research_agent.strategies import BASELINE, PRESETS, StrategyRetriever, config, digest
from research_agent.workbench_store import WorkbenchStore
from research_agent.workbench import JobCancelled, Workbench


class EvidenceStrategyTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root=Path(temp.name);self.store=WorkbenchStore(self.root/'state.db')
        self.sid=self.store.save_space({'name':'sources'})['id']
        self.chat=self.store.create_conversation(self.sid,'independent')['id']
        self.lib=Library(self.store);self.base=Retriever(self.store,self.lib)
        cfg=PRESETS['evidence_miss']
        self.snapshot={'id':'candidate','config':cfg,'config_hash':digest(cfg)}
        self.model=Mock(usage_records=[],request_deadline=None,usage_purpose='answer',name='fixture')
        self.document('source','alpha repeated distractor')
        self.target=self.document('support','alpha '+('context '*180)+'The measured gain was 17 percent.')

    def document(self,title,text,sid=None):
        return self.lib.save(sid or self.sid,{'kind':'paper','title':title,'url':'','canonical_id':title,
            'metadata':{},'data':None,'chunks':[{'text':text,'page':2,'section':'method','line_start':1,'line_end':2}],
            'warnings':[],'boundary':'test original'})[0]

    def adapter(self,**kwargs):
        return StrategyRetriever(self.base,self.snapshot,self.model,question='What was the measured gain?',**kwargs)

    def test_legacy_configuration_is_not_migrated_and_scopes_do_not_trigger_model(self):
        self.assertEqual(config(BASELINE),BASELINE)
        self.assertEqual(len(config(BASELINE)),3)
        for invalid in ({**BASELINE,'other':False},{**BASELINE,'evidence_rerank':1}):
            with self.assertRaises(ValueError):config(invalid)
        with patch('research_agent.evidence_rerank.rerank') as rerank:
            baseline=StrategyRetriever(self.base,{'id':'old','config':BASELINE,'config_hash':digest(BASELINE)},self.model)
            self.assertEqual(baseline.retrieve(self.sid,'alpha')['results'],self.base.retrieve(self.sid,'alpha')['results'])
            adapter=self.adapter()
            adapter.retrieve(self.sid,'alpha',scope='workspace')
            adapter.retrieve(self.sid,'alpha',corpus='history',conversation_id=self.chat)
            adapter.retrieve(self.sid,'alpha',corpus='memory',conversation_id=self.chat)
            rerank.assert_not_called()

    def test_complete_unique_authorized_parents_and_previews_stay_previews(self):
        other=self.store.save_space({'name':'private'})['id']
        self.document('private','alpha private secret',other)
        seen=[]
        def select(question,passages,model):
            self.assertEqual(question,'What was the measured gain?')
            self.assertEqual(len(passages),1)
            self.assertTrue(passages[0]['content'].endswith('17 percent.'))
            self.assertNotIn('private secret',json.dumps(passages))
            seen.extend(passages)
            return {'status':'ok','ranking':[passages[0]['id']]}
        with patch('research_agent.evidence_rerank.rerank',side_effect=select):
            data=self.adapter().retrieve(self.sid,'alpha',artifact_ids=[self.target['id']],top_k=1)
        self.assertEqual(len(data['results']),1)
        hit=data['results'][0]
        self.assertTrue(hit['preview_only']);self.assertEqual(hit['read_with'],'read_evidence')
        self.assertNotIn('17 percent',hit['snippet'])
        self.assertIn('17 percent',self.base.read(self.sid,hit['ref_id'],adjacent=0)['content'])
        self.assertEqual(data['evidence_selection']['supported_passages'],1)

    def test_empty_evidence_is_not_transport_failure_or_baseline_padding(self):
        baseline=self.base.retrieve(self.sid,'alpha')['results']
        self.assertTrue(baseline)
        with patch('research_agent.evidence_rerank.rerank',return_value={'status':'ok','ranking':[]}):
            empty=self.adapter().retrieve(self.sid,'alpha')
        self.assertEqual(empty['results'],[])
        self.assertIn('not proof',empty['guidance'])
        with patch('research_agent.evidence_rerank.rerank',return_value={'status':'fallback','error':'TimeoutError','ranking':[]}):
            failed=self.adapter().retrieve(self.sid,'alpha')
        self.assertEqual(failed['results'],baseline)
        self.assertIn('unavailable',failed['guidance'])

    def test_two_call_limit_cache_restore_and_cancellation_reservation(self):
        checkpoints=[]
        def save(state):checkpoints.append(copy.deepcopy(state))
        passages=[{'id':'x','title':'a','section':'m','content':'original'}]
        with patch('research_agent.evidence_rerank.rerank',return_value={'status':'ok','ranking':['x']}) as rerank:
            first=self.adapter(checkpoint=save)
            self.assertFalse(first._rank('query',passages)['cached'])
            self.assertEqual(checkpoints[0]['calls'][next(iter(checkpoints[0]['calls']))]['status'],'reserved')
            restored=self.adapter(state=checkpoints[-1],checkpoint=save)
            self.assertTrue(restored._rank('query',passages)['cached'])
            changed=[{**passages[0],'content':'changed original'}]
            restored._rank('query',changed)
            self.assertEqual(restored._rank('query',[{**passages[0],'content':'third original'}])['status'],'budget_exhausted')
            self.assertEqual(rerank.call_count,2)
        interrupted=[]
        with patch('research_agent.evidence_rerank.rerank',side_effect=JobCancelled):
            with self.assertRaises(JobCancelled):
                self.adapter(checkpoint=lambda s:interrupted.append(copy.deepcopy(s)))._rank('query',passages)
        with patch('research_agent.evidence_rerank.rerank') as rerank:
            data=self.adapter(state=interrupted[-1])._rank('query',passages)
            self.assertEqual(data['status'],'interrupted_unconfirmed');rerank.assert_not_called()
        self.model.complete.side_effect=JobCancelled
        from research_agent.evidence_rerank import rerank
        with self.assertRaises(JobCancelled):rerank('query',passages,self.model)

    def test_loop_requires_original_read_after_internal_reranking(self):
        class Model:
            name='fixture'
            def complete(model,messages,tools):
                outputs=[json.loads(m['content'])['UNTRUSTED_TOOL_DATA'] for m in messages if m['role']=='tool']
                if not outputs:return ModelDecision('tool_call',tool_name='retrieve',arguments={'query':'alpha','corpus':'documents'})
                last=outputs[-1]
                if last['kind']=='retrieve':
                    self.assertTrue(last['results'][0]['preview_only'])
                    return ModelDecision('tool_call',tool_name='read_evidence',arguments={'ref_id':last['results'][0]['ref_id'],'adjacent':0})
                return ModelDecision('final',content='The measured gain was 17 percent. ['+last['evidence_id']+']')
        model=Model()
        adapter=StrategyRetriever(self.base,self.snapshot,model,question='measured gain')
        def select(question,passages,unused):
            return {'status':'ok','ranking':[p['id'] for p in passages if '17 percent' in p['content']]}
        with patch('research_agent.evidence_rerank.rerank',side_effect=select):
            result=ResearchAgent(None,model,self.root/'traces',retrieval=adapter,space_id=self.sid,
                conversation_id=self.chat,allow_external=False,max_tool_calls=3).run('What was the gain?')
        self.assertEqual(result.tool_calls,2)
        self.assertTrue(any(e.kind.startswith('local:') and '17 percent' in e.content for e in result.evidence))
        self.assertFalse(result.invalid_citations)

    def test_verified_uncertainty_completes_but_unverified_claim_does_not(self):
        class Model:
            name='fixture'
            def complete(model,messages,tools):
                if any(m['role']=='tool' for m in messages):
                    return ModelDecision('final',content='INSUFFICIENT：现有证据不足以确定，不能估算。')
                return ModelDecision('tool_call',tool_name='retrieve',arguments={'query':'unknown','corpus':'documents'})
        check={'ready':True,'claims':[{'block_id':'b','statement':'unknown','supported':True,
            'kind':'uncertainty','evidence_ids':[],'reason':'Appropriately bounded uncertainty'}]}
        with patch('research_agent.loop.check_answer',return_value=check):
            result=ResearchAgent(None,Model(),self.root/'traces',retrieval=self.base,space_id=self.sid,
                allow_external=False,answer_verification=True).run('Unreported value?')
        self.assertEqual((result.status,result.termination),('ok','evidence_insufficient'))
        with patch('research_agent.loop.check_answer',side_effect=TimeoutError):
            failed=ResearchAgent(None,Model(),self.root/'traces',retrieval=self.base,space_id=self.sid,
                allow_external=False,answer_verification=True).run('Unreported value?')
        self.assertEqual(failed.status,'insufficient')
        self.assertEqual(failed.termination,'answer_verification_unavailable')

    def test_workbench_resume_reuses_paid_response_without_double_tool_charge(self):
        class Model:
            name='fixture'
            def complete(model,messages,tools):
                if any(m['role']=='tool' for m in messages):
                    return ModelDecision('final',content='INSUFFICIENT：当前候选证据不足以确定。')
                return ModelDecision('tool_call',tool_name='retrieve',arguments={'query':'alpha','corpus':'documents'})
        app=Workbench(self.store,Model,lambda observe:None,self.root/'traces',start_worker=False)
        self.addCleanup(app.close)
        self.store.message(self.sid,self.chat,'user','What was the gain?')
        job=self.store.enqueue(self.sid,self.chat,'What was the gain?','local',[],kind='LOCAL_QA',research_effort='quick')
        job=self.store.claim_next()
        original_rank=StrategyRetriever._rank
        crashed=False
        def crash_after_receipt(adapter,*args):
            nonlocal crashed
            result=original_rank(adapter,*args)
            if not crashed:
                crashed=True
                raise RuntimeError('simulated crash after durable paid response')
            return result
        with patch.object(app.strategies,'pin',return_value=self.snapshot), patch('research_agent.evidence_rerank.rerank',return_value={'status':'ok','ranking':[]}) as rerank:
            with patch.object(StrategyRetriever,'_rank',crash_after_receipt):
                app.execute(job)
            self.assertEqual(self.store.job(self.sid,job['id'])['status'],'failed')
            app.resume(self.sid,job['id'])
            app.execute(self.store.claim_next())
            self.assertEqual(rerank.call_count,1)
        with closing(self.store._connect()) as db:
            saved=json.loads(db.execute("SELECT payload FROM conversation_events WHERE job_id=? AND kind='harness_retrieval' ORDER BY id DESC LIMIT 1",(job['id'],)).fetchone()[0])
            checkpoint=json.loads(db.execute("SELECT payload FROM conversation_checkpoints WHERE job_id=? AND kind='execution' ORDER BY id DESC LIMIT 1",(job['id'],)).fetchone()[0])
        self.assertEqual(len(saved['calls']),1)
        self.assertEqual(checkpoint['counters'][:2],[1,1])


if __name__=='__main__':unittest.main()
