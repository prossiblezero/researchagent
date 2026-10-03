"""Regression cases for the observed body/neighbor/finalization/transport failures."""
import io
import json
import ssl
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from urllib.error import HTTPError, URLError
from unittest.mock import Mock, patch

from research_agent import search
from research_agent.contracts import Evidence, ModelDecision, ReadResponse, SearchResponse
from research_agent.library import Library
from research_agent.loop import ResearchAgent
from research_agent.models import OpenAICompatibleModel
from research_agent.retrieval import Retriever, digest
from research_agent.verify import check_answer, answer_blocks
from research_agent.workbench_store import WorkbenchStore
from evals.agent_metrics import MeteredModel, usage_summary


class RepairTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)

    def test_late_html_body_survives_download_and_navigation_is_not_evidence(self):
        body=b'<html><script>'+b'x'*225000+b'</script><main><p>Files above 100 MB are not supported.</p></main></html>'
        def fetch(url,addresses,timeout,limit):
            return search._NetworkResponse(200,{'content-type':'text/html'},body[:limit],len(body)>limit)
        with patch.object(search,'resolve_public_addresses',return_value=[]),patch.object(search,'_request_once',side_effect=fetch):
            fixed=search.HttpReader().read('https://example.org/document')
            clipped=search.HttpReader(max_bytes=200000).read('https://example.org/document')
        self.assertTrue(fixed.ok);self.assertIn('100 MB',fixed.content)
        self.assertFalse(clipped.ok);self.assertEqual(clipped.error['code'],'incomplete_body')
        for html in ('<nav>Menu</nav>','<a>Skip to main content</a><div>Expand sidebar</div>'):
            self.assertFalse(search._text_response('x','x',html,'text/html',1,False).ok)

    def test_neighbors_cross_original_chunks_keep_page_and_scope(self):
        store=WorkbenchStore(self.root/'state.db');lib=Library(store);retriever=Retriever(store,lib)
        space=store.save_space({'name':'A'})['id'];other=store.save_space({'name':'B'})['id']
        for sid,title in ((space,'Target'),(other,'Secret')):
            lib.save(sid,{'kind':'paper','title':title,'url':'','canonical_id':title,'metadata':{},'data':None,
                'chunks':[{'text':text,'page':page,'section':'method','line_start':1,'line_end':1} for page,text in
                    [(1,'The layout architecture is RT-DETR.'),(2,'Layout quality is measured on document datasets.'),(3,'TableFormer handles table structure.')]],
                'warnings':[],'boundary':'fixture'})
        with patch.dict('os.environ',{'RETRIEVAL_MODE':'lexical'}):
            hits=retriever.retrieve(space,'quality')['results'];read=retriever.read(space,hits[0]['ref_id'])
        self.assertEqual(read['page'],2)
        self.assertEqual([n['page'] for n in read['neighbors']],[1,3])
        self.assertIn('RT-DETR',read['neighbors'][0]['content'])
        for n in read['neighbors']:
            self.assertEqual(n['artifact_id'],read['artifact_id']);self.assertEqual(n['content_hash'],digest(n['content']))
        with self.assertRaises(ValueError):retriever.read(other,hits[0]['ref_id'])
        with closing(store._connect()) as db,db:
            db.execute('UPDATE document_chunks SET content=? WHERE id=?',('changed',read['neighbors'][0]['chunk_id']))
        with self.assertRaises(ValueError):retriever.read(space,hits[0]['ref_id'])

    @staticmethod
    def judgment(ready,statement='The limit is 100 MB.',ids=None):
        return {'requirements':[{'id':'R1','requirement':'limit','addressed':ready,'block_ids':['$first'],'reason':'covered' if ready else 'read the original'}],
            'blocks':[{'block_id':'$first','kind':'fact','evidence_ids':ids or ['E2'],'supported':ready,'reason':'Original body supports the limit.'}]}

    def make_model(self,decisions,judgments):
        choices=iter(decisions);checks=iter(judgments)
        class Model:
            name='repair-test';supports_answer_verification=True;usage_purpose='answer'
            def complete(inner,messages,tools):
                if inner.usage_purpose=='answer_verification':
                    data=json.loads(messages[-1]['content'])['UNTRUSTED_DOCUMENT_DATA']
                    return ModelDecision('final',content=json.dumps(next(checks)).replace('$first',data['blocks'][0]['block_id']))
                return next(choices)
        return Model()

    def test_draft_must_read_source_then_revision_and_resume_do_not_repeat_tools(self):
        provider=Mock();provider.search.return_value=SearchResponse(True,'limit',[{'title':'Docs','url':'https://example.org/doc','snippet':'The limit is 100 MB.'}])
        reader=Mock();reader.read.return_value=ReadResponse(True,'https://example.org/doc','The limit is 100 MB.')
        model=self.make_model([ModelDecision('tool_call',query='limit',call_id='s'),ModelDecision('final',content='The limit is 100 MB. [E1]'),
            ModelDecision('tool_call',tool_name='read',url='https://example.org/doc',call_id='r'),ModelDecision('final',content=json.dumps({'replace':[{'block_id':answer_blocks('The limit is 100 MB. [E1]')[0]['block_id'],'text':'The limit is 100 MB. [E2]'}],'append':[]}))],
            [self.judgment(False),self.judgment(True)])
        states=[]
        agent=ResearchAgent(provider,model,self.root/'traces',reader=reader,max_tool_calls=4,state_callback=lambda s:states.append(json.loads(json.dumps(s))))
        result=agent.run('What is the limit?')
        self.assertEqual(result.status,'ok');self.assertEqual(result.tool_calls,2)
        self.assertEqual(reader.read.call_count,1);self.assertEqual(provider.search.call_count,1)
        self.assertTrue(states[-1]['answer_check']['ready']);self.assertEqual(states[-1]['answer_repair_rounds'],1)
        resumed=ResearchAgent(provider,self.make_model([],[]),self.root/'traces',reader=reader,max_tool_calls=4,resume_state=states[-1]).run('What is the limit?')
        self.assertEqual(resumed.status,'ok');self.assertEqual(reader.read.call_count,1)

    def test_unfixed_draft_stops_after_two_repairs_and_removes_unverified_facts(self):
        draft='The limit is 100 MB.'
        revision=json.dumps({'replace':[{'block_id':answer_blocks(draft)[0]['block_id'],'text':draft}],'append':[]})
        model=self.make_model([ModelDecision('final',content=draft),ModelDecision('final',content=revision),ModelDecision('final',content=revision)],[self.judgment(False)]*3)
        result=ResearchAgent(Mock(),model,self.root/'traces',max_tool_calls=4).run('What is the limit?')
        self.assertEqual(result.termination,'answer_verification_failed');self.assertEqual(result.status,'insufficient')
        self.assertTrue(result.answer.startswith('INSUFFICIENT'))
        self.assertFalse(any(c.status=='SUPPORTED' for c in result.claims))

    def test_checker_cannot_promote_search_preview_to_original(self):
        model=self.make_model([],[self.judgment(True,ids=['E1'])])
        result=check_answer(model,'limit?','The limit is 100 MB. [E1]',[Evidence('E1','S1','The limit is 100 MB.','snippet')],[])
        self.assertFalse(result['ready']);self.assertFalse(result['claims'][0]['supported'])

    def test_checker_uses_original_even_with_additional_preview_citation(self):
        model=self.make_model([],[self.judgment(True,ids=['E1','E2'])])
        result=check_answer(model,'limit?','The limit is 100 MB. [E1] [E2]',[
            Evidence('E1','S1','The limit is 100 MB.','snippet'),Evidence('E2','S1','The limit is 100 MB.','page')],[])
        self.assertTrue(result['ready']);self.assertEqual(result['claims'][0]['evidence_ids'],['E2'])

    def test_checker_does_not_duplicate_full_summaries_into_its_input(self):
        from research_agent.contracts import Source
        evidence=[Evidence(f'E{i}',f'S{i}','Long body. '*1000,'page',summary='Large summary. '*1000) for i in range(1,25)]
        sources=[Source(f'S{i}','Document','https://example.org/'+str(i),'Preview. '*1000) for i in range(1,25)]
        model=self.make_model([],[self.judgment(True,ids=['E2'])])
        result=check_answer(model,'limit?','The limit is 100 MB. [E2]',evidence,sources,input_limit=14000)
        self.assertTrue(result['ready'])

    def test_honest_uncertainty_can_complete_without_inventing_a_citation(self):
        judgment=self.judgment(True,'无法根据现有资料确定该金额，不能据此估算。')
        judgment['blocks'][0].update(kind='uncertainty',evidence_ids=[])
        model=self.make_model([ModelDecision('final',content='无法根据现有资料确定该金额，不能据此估算。')],[judgment])
        result=ResearchAgent(Mock(),model,self.root/'traces',max_tool_calls=2).run('现有资料能否确定金额？')
        self.assertEqual(result.status,'ok')
        self.assertEqual(result.claims[0].status,'INSUFFICIENT')
        self.assertEqual(result.claims[0].evidence_ids,[])

    def test_checker_schema_failure_preserves_explicitly_unverified_draft(self):
        model=self.make_model([ModelDecision('final',content='Unsupported assertion.')]*3,[{'ready':True}]*3)
        result=ResearchAgent(Mock(),model,self.root/'traces',max_tool_calls=2).run('Find a fact')
        self.assertEqual(result.status,'insufficient');self.assertIn('Unsupported assertion.',result.answer)
        self.assertIn('待核验草稿',result.answer)
        self.assertEqual(result.termination,'answer_verification_unavailable')

    def response(self):
        response=Mock();response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False)
        response.read.return_value=json.dumps({'usage':{'prompt_tokens':10,'completion_tokens':4,'total_tokens':14},
            'choices':[{'message':{'content':'ok'},'finish_reason':'stop'}]}).encode()
        return response

    def test_transient_retries_are_metered_per_physical_request(self):
        model=OpenAICompatibleModel('https://example.org/v1','test','test');ledger=[]
        meter=MeteredModel(model,ledger,'research',self.root,'retry')
        failure=HTTPError('https://example.org',429,'busy',{'Retry-After':'99'},io.BytesIO(b'busy'))
        with patch('research_agent.models.urlopen',side_effect=[URLError(ssl.SSLEOFError('EOF')),failure,self.response()]) as transport,patch('research_agent.models.time.sleep') as delay:
            self.assertEqual(meter.complete([{'role':'user','content':'q'}],[]).content,'ok')
        self.assertEqual(transport.call_count,3);self.assertEqual(len(ledger),3)
        self.assertEqual(delay.call_args_list[1].args,(10.0,))
        self.assertEqual(usage_summary(ledger)['unknown_usage_requests'],2)
        self.assertEqual(usage_summary(ledger)['known_token_subtotal']['total_tokens'],14)
        self.assertEqual([r['attempt'] for r in model.usage_records],[1,2,3])
        self.assertTrue(meter.supports_answer_verification)

    def test_auth_bad_json_and_tls_certificate_errors_are_not_retried(self):
        for error in (HTTPError('https://example.org',401,'denied',{},io.BytesIO(b'no')),
                      URLError(ssl.SSLCertVerificationError('certificate invalid'))):
            with patch('research_agent.models.urlopen',side_effect=error) as transport,patch('research_agent.models.time.sleep') as sleep:
                with self.assertRaises(RuntimeError):OpenAICompatibleModel('https://example.org','test','test').complete([],[])
                self.assertEqual(transport.call_count,1);sleep.assert_not_called()
        response=self.response();response.read.return_value=b'bad json'
        with patch('research_agent.models.urlopen',return_value=response) as transport:
            with self.assertRaises(RuntimeError):OpenAICompatibleModel('https://example.org','test','test').complete([],[])
            self.assertEqual(transport.call_count,1)

    def test_transient_failure_is_bounded(self):
        with patch('research_agent.models.urlopen',side_effect=TimeoutError('timeout')) as transport,patch('research_agent.models.time.sleep'):
            model=OpenAICompatibleModel('https://example.org','test','test')
            with self.assertRaises(RuntimeError):model.complete([],[])
        self.assertEqual(transport.call_count,3);self.assertEqual(len(model.usage_records),3)


if __name__=='__main__':unittest.main()
