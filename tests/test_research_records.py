"""V3 contracts: immutable snapshots, provenance, isolation and honest comparisons."""
import copy
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from research_agent.contracts import Claim, Evidence, Source, RunResult, ModelDecision
from research_agent.library import Library
from research_agent.research_records import ResearchRecords, originals
from research_agent.workbench_store import WorkbenchStore, Conflict, NotFound
from research_agent.trace import now_iso
from research_agent.sessions import Sessions
from research_agent.memory import Memory
from research_agent.verify import evidence_role


class RecordsTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.store=WorkbenchStore(self.root/'state.db')
        self.space=self.store.save_space({'name':'research'})['id']
        self.other=self.store.save_space({'name':'unrelated'})['id']
        self.chat=self.store.create_conversation(self.space,'facts')['id']
        self.library=Library(self.store);self.records=ResearchRecords(self.store,self.library)
        self.store.message(self.space,self.chat,'user','compare papers')
        self.job=self.store.enqueue(self.space,self.chat,'compare papers','facts',[])
        self.store.claim_next();self.job=self.store.job(self.space,self.job['id'])
        self.papers=[]
        for i in (1,2):
            item,_=self.library.save(self.space,{'kind':'paper','title':f'Paper {i}','url':f'https://example.org/paper{i}',
                'canonical_id':f'paper:{i}','metadata':{},'warnings':[],'boundary':'原文片段','data':None,
                'chunks':[{'ordinal':0,'page':i,'section':'method','line_start':1,'line_end':1,'text':f'Paper {i} uses feedback to improve its next action.'}]})
            self.papers.append(item)
        evidence=[]
        for i,paper in enumerate(self.papers,1):
            c=self.library.chunks(self.space,paper['id'])[0]
            evidence.append(Evidence(f'E{i}',f'S{i}',c['content'],kind=f'local:D{c["id"]}-hash',provenance={'artifact_id':paper['id'],'chunk_id':c['id'],'page':i,'space_id':self.space}))
        evidence.append(Evidence('E3','S1','preview only','snippet'))
        self.result=RunResult('Paper 1 uses feedback. [E1]\n\nPaper 2 uses feedback. [E2]',
            [Source(f'S{i}',f'Paper {i}',f'https://example.org/paper{i}','',retrieved_at=now_iso()) for i in (1,2)],
            str(self.root/'trace.jsonl'),'ok','completed',4,['S1','S2'],[],evidence,
            [Claim(f'C{i}',f'Paper {i} uses feedback',[f'E{i}'],'SUPPORTED',None,'original supports statement') for i in (1,2)])
        self.run_id=self.store.save(self.result,'facts',now_iso())
        self.records.capture(self.job,self.run_id,('report.md','report.html'))
        self.version=self.records.report(self.space,self.job['id'])['versions'][0]

    def anchor(self,i=1):
        return {'report_version_id':self.version['id'],'claim_id':f'C{i}','evidence_id':f'E{i}','span':1}

    def experiment_body(self,kind='baseline'):
        return {'kind':kind,'title':'baseline' if kind=='baseline' else 'candidate', 'status':'completed','note':'measured outside workbench',
            'config':{'dataset':'tasks','dataset_version':'v1','split':'validation','split_hash':'frozen-123',
                      'seeds':[42,123],'code_revision':'commit-a','command':'python eval.py','environment':'python 3 CPU','parameters':{}},
            'metrics':[{'name':'accuracy','unit':'percent','direction':'higher','target_mode':'delta','target':2},
                       {'name':'latency','unit':'ms','direction':'lower','target_mode':'relative_percent','target':10}],
            'results':{'accuracy':70,'latency':100},'result_source':'logs/run-1.json, 2026-09-21, external measurement'}

    def test_report_edits_preserve_history_and_reset_automatic_verification(self):
        old=copy.deepcopy(self.version)
        report,new_id=self.records.draft(self.space,self.job['id'],{'base_version_id':old['id'],'content':'New claim [E2]','note':'changed interpretation'})
        self.assertEqual(report['versions'][0],old)
        self.assertEqual(report['versions'][1]['snapshot']['claims'][0]['status'],'UNVERIFIED')
        self.assertEqual(report['versions'][1]['status'],'draft')
        self.assertIn('+New claim',self.records.compare_reports(self.space,self.job['id'],old['id'],new_id)['diff'])
        with self.assertRaises(Conflict):self.records.draft(self.space,self.job['id'],{'base_version_id':old['id'],'content':'stale','note':'race'})
        with self.assertRaises(ValueError):self.records.draft(self.space,self.job['id'],{'base_version_id':new_id,'content':'bad [E999]','note':'invalid'})

    def test_report_review_publish_order_and_audit(self):
        with self.assertRaises(Conflict):self.records.transition(self.space,self.job['id'],self.version['id'],{'status':'published','note':'skip review'})
        for status in ('reviewed','published'):
            report=self.records.transition(self.space,self.job['id'],self.version['id'],{'status':status,'note':'checked facts against originals'})
        self.assertEqual(report['versions'][0]['status'],'published')
        self.assertEqual([e['action'] for e in report['events']],['draft_created','reviewed','published'])
        self.assertEqual(report['versions'][0]['digest'],self.version['digest'])
        with self.assertRaises(Conflict):self.records.transition(self.space,self.job['id'],self.version['id'],{'status':'reviewed','note':'cannot mutate'})
        text=self.records.report_markdown(self.space,self.job['id'],self.version['id'])
        self.assertIn('Paper 1 uses feedback to improve',text);self.assertIn('人工确认',text)
        from research_agent.reports import render_markdown
        html,_=render_markdown(text)
        self.assertIn('href="#source-E1"',html);self.assertEqual(html.count('id="source-E1"'),1)

    def test_preview_cannot_be_published_or_used_as_anchor(self):
        report,new_id=self.records.draft(self.space,self.job['id'],{'base_version_id':self.version['id'],'content':'Claim [E3]','note':'preview only'})
        with self.assertRaises(Conflict):self.records.transition(self.space,self.job['id'],new_id,{'status':'reviewed','note':'not original'})
        with closing(self.store._connect()) as db:
            with self.assertRaises(ValueError):self.records.anchors(db,self.space,[{**self.anchor(),'evidence_id':'E3'}])
        self.assertEqual(set(originals(self.version['snapshot'])),{'E1','E2'})

    def test_legacy_source_and_local_citations_keep_their_evidence_binding(self):
        chunk=self.version['snapshot']['evidence'][0]['provenance']['chunk_id']
        report,vid=self.records.draft(self.space,self.job['id'],{'base_version_id':self.version['id'],
            'content':f'Legacy source [S1] and local passage [L{chunk}]','note':'legacy citations'})
        version=report['versions'][-1]
        self.assertEqual(self.records.coverage(version)['original_cited'],1)
        self.assertIn('E1',version['snapshot']['claims'][0]['evidence_ids'])
        self.records.transition(self.space,self.job['id'],vid,{'status':'reviewed','note':'reviewed original'})
        with self.assertRaises(ValueError):self.records.draft(self.space,self.job['id'],{'base_version_id':vid,'content':'unknown [L999999]','note':'invalid'})

    def test_independent_conversations_share_reports_without_history_inheritance(self):
        chat=self.store.create_conversation(self.space,'independent')['id']
        self.assertEqual(self.store.history(self.space,chat),[])
        self.assertEqual(len(self.records.reports(self.space)),1)
        with self.assertRaises(NotFound):self.records.report(self.other,self.job['id'])
        self.assertEqual(self.records.reports(self.other),[])
        self.store.finish(self.job['id'],'completed',self.result.answer,run_id=self.run_id)
        sessions=Sessions(self.store,Memory(self.store));sessions.delete_branch(self.space,self.chat)
        self.assertEqual(self.records.reports(self.space),[])
        with self.assertRaises(NotFound):self.records.report(self.space,self.job['id'])
        sessions.restore_branch(self.space,self.chat)
        self.assertEqual(len(self.records.reports(self.space)),1)

    def test_graph_edges_require_scoped_actual_claim_evidence_bindings(self):
        method=self.records.entity(self.space,{'kind':'method','title':'feedback','origin':{}})
        dataset=self.records.entity(self.space,{'kind':'dataset','title':'tasks','origin':{}})
        graph=self.records.relation(self.space,{'source_id':method['id'],'target_id':dataset['id'],'relation':'proposed evaluation','status':'HYPOTHESIS','anchors':[self.anchor()]})
        self.assertEqual(graph['edges'][0]['status'],'HYPOTHESIS')
        self.assertIn('Paper 1 uses feedback',graph['edges'][0]['anchors'][0]['quote'])
        self.assertEqual(len(graph['available_anchors']),2)
        self.assertEqual(graph['available_anchors'][0]['value']['span'],1)
        with self.assertRaises(NotFound):self.records.relation(self.other,{'source_id':method['id'],'target_id':dataset['id'],'relation':'bad','anchors':[self.anchor()]})
        with closing(self.store._connect()) as db:
            with self.assertRaises(ValueError):self.records.anchors(db,self.space,[{**self.anchor(),'evidence_id':'E2'}])
            with self.assertRaises(ValueError):self.records.anchors(db,self.space,[self.anchor()],paper_pair=True)
            self.assertEqual(len(self.records.anchors(db,self.space,[self.anchor(1),self.anchor(2)],paper_pair=True)),2)

    def test_experiment_versions_pin_baseline_and_compare_without_execution(self):
        baseline=self.records.save_experiment(self.space,self.experiment_body());bv=baseline['versions'][0]
        body=self.experiment_body('experiment');body.update(baseline_version_id=bv['id'],results={'accuracy':73,'latency':85})
        experiment=self.records.save_experiment(self.space,body);ev=experiment['versions'][0]
        comparison=self.records.compare_experiments(self.space,bv['id'],ev['id'])
        self.assertEqual(comparison['outcome'],'met');self.assertEqual(comparison['metrics'][1]['improvement'],15)
        body.update(base_version_id=ev['id'],results={'accuracy':71,'latency':99},note='another run')
        newer=self.records.save_experiment(self.space,body,experiment['id'])
        self.assertEqual(newer['versions'][0],ev)
        self.assertEqual(self.records.compare_experiments(self.space,bv['id'],newer['versions'][1]['id'])['outcome'],'not_met')
        comparison=self.records.compare_experiments(self.space,ev['id'],newer['versions'][1]['id'])
        self.assertEqual(comparison['outcome'],'comparison_only')
        self.assertTrue(all(m['target_met'] is None for m in comparison['metrics']))
        export=self.records.handoff(self.space,ev['id'])
        self.assertFalse(export['execution_enabled']);self.assertEqual(export['baseline_version']['id'],bv['id'])
        with self.assertRaises(Conflict):self.records.save_experiment(self.space,body,experiment['id'])
        with self.assertRaises(NotFound):self.records.handoff(self.other,ev['id'])

    def test_changed_dataset_or_units_cannot_be_claimed_as_improvement(self):
        baseline=self.records.save_experiment(self.space,self.experiment_body())['versions'][0]
        for key,value in [('split','test'),('dataset_version','v2'),('seeds',[99]),('split_hash','other')]:
            body=self.experiment_body('experiment');body['config'][key]=value;body['baseline_version_id']=baseline['id']
            version=self.records.save_experiment(self.space,body)['versions'][0]
            c=self.records.compare_experiments(self.space,baseline['id'],version['id'])
            self.assertEqual(c['outcome'],'incomparable');self.assertIsNone(c['metrics'][0]['improvement'])
        body=self.experiment_body('experiment');body['metrics'][0]['unit']='ratio';body['baseline_version_id']=baseline['id']
        version=self.records.save_experiment(self.space,body)['versions'][0]
        self.assertFalse(self.records.compare_experiments(self.space,baseline['id'],version['id'])['comparable'])

    def test_zero_baseline_and_partial_failed_results_remain_unknown(self):
        body=self.experiment_body();body['results']['latency']=0
        baseline=self.records.save_experiment(self.space,body)['versions'][0]
        body=self.experiment_body('experiment');body['baseline_version_id']=baseline['id']
        version=self.records.save_experiment(self.space,body)['versions'][0]
        c=self.records.compare_experiments(self.space,baseline['id'],version['id'])
        self.assertEqual(c['outcome'],'undetermined');self.assertIsNone(c['metrics'][1]['relative_percent'])
        body.update(status='failed',results={},result_source='',note='timeout')
        version=self.records.save_experiment(self.space,body)['versions'][0]
        self.assertEqual(self.records.compare_experiments(self.space,baseline['id'],version['id'])['outcome'],'failed')

    def test_invalid_measurements_and_unproven_results_rejected(self):
        for value in (True,float('nan'),float('inf'),'71'):
            body=self.experiment_body();body['results']['accuracy']=value
            with self.assertRaises(ValueError):self.records.save_experiment(self.space,body)
        for update in ({'status':'planned'},{'result_source':''},{'results':{'accuracy':70}},{'baseline_version_id':'unknown'}):
            body=self.experiment_body();body.update(update)
            with self.assertRaises(ValueError):self.records.save_experiment(self.space,body)
        self.assertEqual(self.records.experiments(self.space),[])

    def test_hypothesis_generation_retains_two_originals_and_records_failed_attempt(self):
        candidate={'title':'gated feedback','status':'HYPOTHESIS','gap':'在已读两篇中未评估的组合','hypothesis':'可能提高成功率，待验证',
            'method_change':'加入验证门控','expected_benefit':'预期降低错误动作，尚未测量','risks':'延迟可能增加','validation_plan':'固定验证集和种子，对比原始基线与消融，以成功率和延迟检验',
            'anchors':[{k:v for k,v in self.anchor(i).items() if k!='report_version_id'} for i in (1,2)]}
        answers=iter([{'ideas':[{**candidate,'status':'SUPPORTED'}]},{'ideas':[candidate]},
            {'checks':[{'index':0,'grounded':True,'hypothesis_only':True,'testable':True,'reason':'supported by excerpts'}]}])
        class Model:
            def complete(self,messages,tools):return ModelDecision('final',json.dumps(next(answers),ensure_ascii=False))
        events=[]
        values=self.records.generate_ideas(self.job,self.version['id'],Model(),lambda k,v:events.append(k))
        self.assertIn('idea_rejected',events);self.assertEqual(values[0]['status'],'HYPOTHESIS')
        saved=self.records.save_ideas(self.job,self.version['id'],values)
        self.assertEqual(self.store.job(self.space,self.job['id'])['status'],'completed')
        self.assertEqual(len(self.records.ideas(self.space)),1)
        self.assertEqual(Memory(self.store).list(self.space),[])
        self.assertEqual(self.records.ideas(self.other),[])

    def test_repeated_migration_keeps_original_rows(self):
        with closing(self.store._connect()) as db:
            tables=['research_spaces','conversations','messages','runs','evidence','reports','report_versions']
            before={t:list(map(tuple,db.execute('SELECT * FROM '+t))) for t in tables}
        WorkbenchStore(self.store.path);WorkbenchStore(self.store.path)
        with closing(self.store._connect()) as db:
            self.assertEqual(before,{t:list(map(tuple,db.execute('SELECT * FROM '+t))) for t in tables})
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0],'ok')

    def test_cancelled_generation_cannot_commit_candidates(self):
        with closing(self.store._connect()) as db,db:
            db.execute("UPDATE research_jobs SET status='cancelled' WHERE id=?",(self.job['id'],))
        with self.assertRaises(Conflict):self.records.save_ideas(self.job,self.version['id'],[])
        self.assertEqual(self.records.ideas(self.space),[])
        self.assertEqual(len(self.records.report(self.space,self.job['id'])['versions']),1)

    def test_report_hypotheses_are_retrievable_history_never_primary_paper_evidence(self):
        self.library.sync_reports(self.space)
        reports=[a for a in self.library.list(self.space) if a['kind']=='report']
        self.assertTrue(reports)
        from research_agent.retrieval import Retriever
        retriever=Retriever(self.store,self.library)
        retriever._dense_attempted=True
        result=retriever.retrieve(self.space,'Paper feedback',artifact_ids=[reports[0]['id']])
        hit=result['results'][0];read=retriever.read(self.space,hit['ref_id'],adjacent=0)
        self.assertEqual(read['artifact_kind'],'report')
        e=Evidence('E1','S1',read['content'],kind='local:'+read['ref_id'],provenance={'artifact_kind':read['artifact_kind']})
        self.assertEqual(evidence_role(e),'history')

    def test_http_records_scope_and_version_exports(self):
        import threading
        from urllib.request import urlopen,Request
        from urllib.error import HTTPError
        from server import make_server
        from research_agent.workbench import OfflineRouter
        self.store.finish(self.job['id'],'completed',self.result.answer,run_id=self.run_id)
        server=make_server(port=0,db_path=self.store.path,trace_dir=self.root/'traces',model_factory=OfflineRouter,start_worker=False)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.addCleanup(thread.join,3);self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        address=f'http://127.0.0.1:{server.server_port}'
        def api(path,body=None,method=None):
            with urlopen(Request(address+path,data=json.dumps(body).encode() if body is not None else None,method=method,headers={'Content-Type':'application/json'}),timeout=5) as r:return json.load(r)
        prefix=f'/api/spaces/{self.space}/research-records'
        self.assertEqual(len(api(prefix+'/reports')['reports']),1)
        body=self.experiment_body();baseline=api(prefix+'/experiments',body)
        version=baseline['versions'][0]['id']
        self.assertFalse(api(prefix+'/experiment-versions/'+version+'/handoff')['execution_enabled'])
        with self.assertRaises(HTTPError) as error:api(f'/api/spaces/{self.other}/research-records/experiment-versions/{version}/handoff')
        self.assertEqual(error.exception.code,404)
        body['results']['accuracy']='not a number'
        with self.assertRaises(HTTPError) as error:api(prefix+'/experiments',body)
        self.assertEqual(error.exception.code,400)
        with urlopen(address+prefix+'/reports/'+self.job['id']+'/versions/'+self.version['id']+'/report.html') as r:
            self.assertIn('Paper 1 uses feedback',r.read().decode())
        for path in ('/v3-evaluation/%2e%2e/%2e%2e/%2e%2e/.env','/v3-evaluation/final-live-case/state.sqlite'):
            with self.assertRaises(HTTPError) as error:api(path)
            self.assertEqual(error.exception.code,404)


if __name__=='__main__':unittest.main()
