"""Reading continuity must survive controller context trimming and file revisions."""
import copy,json,unittest
from pathlib import Path
from tests import test_auto_research as support
from research_agent.auto_research import AutoResearch,TOOLS
from research_agent.context import _size

class ReadingProgressTests(unittest.TestCase):
    def setUp(self):
        self.case=support.AutoResearchTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.job=self.case.start(budget={'decisions':36})
        self.job=self.case.store.claim_next()
        self.state=self.case.state(self.job)
        self.config=json.loads(self.job['payload'])['auto_research']
        self.workspace=self.case.app.coding.work_root/self.job['id']
        self.workspace.mkdir(parents=True,exist_ok=True)
        self.serial=0

    def inspect(self,path,offset):
        self.serial+=1
        decision={'tool_name':'inspect','arguments':{'kind':'file','id':'current','path':path,'offset':offset},
                  'call_id':'read-'+str(self.serial)}
        result=self.case.app.auto_research.dispatch(self.job,self.state,decision,self.config)
        self.assertIn('content',result)
        self.state['pending']=decision
        self.case.app.auto_research.complete_tool(self.job,self.state,result)

    def test_visible_coverage_keeps_gaps_and_resets_for_changed_source(self):
        target=self.workspace/'method.py'
        target.write_bytes(b'a'*20000)
        for offset in (0,3600,10000,3500):self.inspect('method.py',offset)
        before=copy.deepcopy(self.state['messages'])
        result=AutoResearch.reading_progress(before)
        row=result['files'][0]
        self.assertEqual((row['covered_source_chars'],row['next_unread_offset'],row['repeated_source_chars']),(10800,7200,3600))
        self.assertEqual(row['read_calls'],4)
        self.assertFalse(row['complete'])
        self.assertEqual(before,self.state['messages'])
        old_hash=row['sha256']
        target.write_bytes(b'b'*20000)
        self.inspect('method.py',1)
        row=AutoResearch.reading_progress(self.state['messages'])['files'][0]
        self.assertNotEqual(old_hash,row['sha256'])
        self.assertEqual((row['covered_source_chars'],row['next_unread_offset'],row['read_calls']),(3600,0,1))

    def test_context_trim_preserves_progress_and_last_decision(self):
        for index in range(4):
            path='source-'+str(index)+'.py'
            (self.workspace/path).write_bytes(bytes([65+index])*27000)
            for offset in range(0,27000,3600):self.inspect(path,offset)
        self.state['context']={}
        self.state['decisions']=35
        durable=copy.deepcopy(self.state['messages'])
        self.assertGreater(_size(durable,TOOLS)[1],28000)
        self.case.model.complete.return_value=support.call('finish_research',{'outcome':'reported','summary':'Bounded report','limitations':''},99)
        self.case.app.auto_research.decide(self.job,self.state,self.config,lambda *args,**kwargs:None)
        prompt=self.case.model.complete.call_args.args[0]
        overview=json.loads(prompt[1]['content'])
        self.assertEqual(overview['remaining_decisions_including_current'],1)
        self.assertEqual(len(overview['reading_progress']['files']),4)
        self.assertTrue(all(row['complete'] for row in overview['reading_progress']['files']))
        self.assertEqual(self.state['messages'],durable)
        self.assertLess(len(prompt),len(durable))
        self.assertLessEqual(_size(prompt,TOOLS)[1],32000)

    def test_redaction_keeps_original_span_cursor_after_sqlite_reload(self):
        (self.workspace/'paper.txt').write_text('basic content\n'+'z'*6000,encoding='utf-8')
        self.inspect('paper.txt',0)
        restored=self.case.state(self.job)
        value=json.loads(restored['messages'][-1]['content'])['UNTRUSTED_TOOL_DATA']
        self.assertIn('[REDACTED]',value['content'])
        self.assertNotEqual(len(value['content']),value['source_span_chars'])
        row=AutoResearch.reading_progress(restored['messages'])['files'][0]
        self.assertEqual((row['covered_source_chars'],row['next_unread_offset']),(3600,3600))
        self.assertEqual(row['returned_text_chars'],len(value['content']))

    def test_legacy_or_unrelated_payloads_cannot_claim_current_reading(self):
        (self.workspace/'method.py').write_bytes(b'x'*100)
        self.inspect('method.py',0)
        history=copy.deepcopy(self.state['messages'])
        payload=json.loads(history[-1]['content'])
        payload['UNTRUSTED_TOOL_DATA'].pop('sha256',None)
        history[-1]['content']=json.dumps(payload)
        self.assertEqual(AutoResearch.reading_progress(history)['files'],[])
        history=copy.deepcopy(self.state['messages'])
        history[-2]['tool_calls'][0]['function']['name']='coding_agent'
        self.assertEqual(AutoResearch.reading_progress(history)['files'],[])
        history=copy.deepcopy(self.state['messages'])
        payload=json.loads(history[-1]['content'])
        payload['UNTRUSTED_TOOL_DATA']['next_offset']=90
        history[-1]['content']=json.dumps(payload)
        self.assertEqual(AutoResearch.reading_progress(history)['files'],[])

if __name__=='__main__':unittest.main()
