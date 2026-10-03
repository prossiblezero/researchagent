"""Workspace sharing, independent conversations, ancestry and recoverable deletion."""
import json
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock,patch
from urllib.request import Request, urlopen

from research_agent.workbench_store import WorkbenchStore, Conflict, NotFound
from research_agent.sessions import Sessions, initialize
from research_agent.memory import Memory
from research_agent.library import Library
from research_agent.retrieval import Retriever
from server import make_server


class SessionModelTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.store=WorkbenchStore(self.root/'state.db')
        self.a=self.store.save_space({'name':'检索设计'})['id']
        self.b=self.store.save_space({'name':'新研究'})['id']
        self.ca=self.store.create_conversation(self.a,'主线')['id']
        self.cb=self.store.create_conversation(self.b,'主线')['id']
        self.memory=Memory(self.store);self.sessions=Sessions(self.store,self.memory)
        self.lib=Library(self.store);self.retriever=Retriever(self.store,self.lib)

    def turn(self,text='原问题',answer='原回答'):
        user=self.store.message(self.a,self.ca,'user',text)
        reply=self.store.message(self.a,self.ca,'assistant',answer)
        return user,reply

    def test_legacy_migration_is_idempotent_and_preserves_all_messages(self):
        self.turn();other=self.store.create_conversation(self.a,'旧独立聊天')['id']
        self.store.message(self.a,other,'user','独立历史')
        with closing(self.store._connect()) as db,db:
            before=[tuple(r) for r in db.execute('SELECT id,conversation_id,content FROM messages ORDER BY id')]
            db.execute('UPDATE conversations SET created_at=? WHERE id=?',('2000-01-01',self.ca))
            db.execute('UPDATE research_spaces SET main_branch_id=NULL')
            initialize(db);initialize(db)
            self.assertEqual(db.execute('SELECT main_branch_id FROM research_spaces WHERE id=?',(self.a,)).fetchone()[0],self.ca)
            self.assertEqual(before,[tuple(r) for r in db.execute('SELECT id,conversation_id,content FROM messages ORDER BY id')])

    def test_historical_user_and_assistant_forks_do_not_inherit_future_memory(self):
        first,reply=self.turn()
        future,_=self.turn('记住：后续决定 FUTURE_ONLY','后续回答')
        self.memory.explicit(self.a,future)
        branch=self.sessions.fork(self.a,self.ca,through_message_id=reply['id'])
        self.assertEqual(len(self.store.history(self.a,branch['id'])),2)
        self.assertEqual(self.memory.snapshot(self.a,branch['id']),[])
        self.assertEqual(self.retriever.retrieve(self.a,'FUTURE_ONLY',corpus='memory',conversation_id=branch['id'])['results'],[])
        edited=self.sessions.fork(self.a,self.ca,through_message_id=future['id'])
        self.assertEqual(edited['draft'],future['content'])
        self.assertEqual(len(self.store.history(self.a,edited['id'])),2)
        sent=self.store.message(self.a,edited['id'],'user','修改的问题')
        self.assertEqual(sent['turn_seq'],2)
        self.assertEqual(len(self.store.history(self.a,edited['id'])),3)

    def test_deleted_turn_hides_job_memory_and_report_but_preserves_independent_fork(self):
        user=self.store.message(self.a,self.ca,'user','记住：报告顺序 SCOPE_TOKEN')
        self.memory.explicit(self.a,user)
        job=self.store.enqueue(self.a,self.ca,user['content'],user['content'],[])
        self.store.cancel(self.a,job['id'])
        branch=self.sessions.fork(self.a,self.ca)
        record={'kind':'report','title':'保留文件','url':'','canonical_id':'report:fixture','metadata':{},'data':None,
                'chunks':[{'text':'SCOPE_TOKEN report','page':None,'section':'body','line_start':1,'line_end':1}], 'warnings':[],'boundary':'test'}
        artifact=self.lib.save(self.a,record,job_id=job['id'])[0]
        hit=self.retriever.retrieve(self.a,'SCOPE_TOKEN',corpus='memory')['results'][0]
        self.sessions.delete_turn(self.a,self.ca,user['id'])
        self.assertEqual(self.store.history(self.a,self.ca),[])
        self.assertTrue(self.store.history(self.a,branch['id']))
        self.assertEqual(self.store.jobs(self.a),[])
        self.assertEqual(self.memory.snapshot(self.a,self.ca),[])
        self.assertEqual(self.lib.list(self.a),[])
        with self.assertRaises(NotFound):self.store.retry(self.a,job['id'])
        with self.assertRaises(NotFound):self.lib.get(self.a,artifact['id'])
        with self.assertRaises(ValueError):self.retriever.read(self.a,hit['ref_id'])
        self.sessions.restore_turn(self.a,self.ca,user['turn_seq'])
        self.assertTrue(self.store.jobs(self.a));self.assertTrue(self.lib.list(self.a))
        self.assertTrue(self.memory.snapshot(self.a,self.ca))

    def test_archive_trash_restore_preserve_research_and_scope(self):
        self.turn('旧决定 ZXBACKUP','选择本地索引')
        self.store.set_space_state(self.a,'archived')
        self.assertNotIn(self.a,[s['id'] for s in self.store.spaces()])
        hits=self.retriever.retrieve(self.b,'ZXBACKUP',corpus='history',conversation_id=self.cb,scope='all_sessions')['results']
        self.assertEqual(hits[0]['source_space_name'],'检索设计')
        self.store.delete_space(self.a)
        with self.assertRaises(ValueError):self.retriever.read(self.b,hits[0]['ref_id'],allow_cross=True)
        self.assertEqual(self.retriever.retrieve(self.b,'ZXBACKUP',corpus='history',scope='all_sessions')['results'],[])
        self.store.set_space_state(self.a,'active')
        self.assertEqual(len(self.store.history(self.a,self.ca)),2)
        self.assertIn('ZXBACKUP',self.retriever.read(self.b,hits[0]['ref_id'],allow_cross=True)['content'])

    def test_archiving_an_idle_area_does_not_interrupt_another_research(self):
        job=self.store.enqueue(self.b,self.cb,'research','research',[])
        self.store.claim_next()
        self.store.set_space_state(self.a,'archived')
        self.store.set_space_state(self.a,'active')
        self.assertEqual(self.store.job(self.b,job['id'])['status'],'running')

    def test_queued_future_local_preference_does_not_enter_earlier_turn(self):
        first,_=self.turn()
        future=self.store.message(self.a,self.ca,'user','记住：之后才设置的格式')
        self.memory.explicit(self.a,future)
        self.assertEqual(self.memory.snapshot(self.a,self.ca,turn_seq=first['turn_seq']),[])
        self.assertTrue(self.memory.snapshot(self.a,self.ca,turn_seq=future['turn_seq']))

    def test_explicitly_restricted_empty_area_completes_without_search_or_invention(self):
        from research_agent.workbench import Workbench,OfflineRouter
        self.turn('Cedar uses SQLite','Source record')
        app=Workbench(self.store,OfflineRouter,Mock(),self.root/'traces',start_worker=False)
        try:
            response=app.send(self.b,self.cb,'仅当前研究区的本地记录：Cedar 方案是什么？不要跨研究区。')
            with patch('research_agent.loop.ResearchAgent.run',side_effect=AssertionError('empty area needs no model investigation')):
                app.execute(self.store.claim_next())
            job=self.store.job(self.b,response['task_id'])
            self.assertEqual(job['status'],'completed');self.assertIn('没有记录',job['summary'])
            self.assertNotIn('SQLite',job['summary'])
        finally:app.close()

    def test_any_conversation_can_be_deleted_without_deleting_its_fork(self):
        self.turn()
        branch=self.sessions.fork(self.a,self.ca)
        self.sessions.delete_branch(self.a,self.ca)
        self.assertTrue(self.store.history(self.a,branch['id']))
        self.assertEqual(self.store.space(self.a)['main_branch_id'],branch['id'])
        self.sessions.restore_branch(self.a,self.ca)
        job=self.store.enqueue(self.a,branch['id'],'active','active',[])
        with self.assertRaises(Conflict):self.sessions.delete_branch(self.a,branch['id'])
        self.store.cancel(self.a,job['id'])
        original=self.store.history(self.a,branch['id'])
        self.sessions.delete_branch(self.a,branch['id'])
        with self.assertRaises(NotFound):self.store.history(self.a,branch['id'])
        self.sessions.restore_branch(self.a,branch['id'])
        self.assertEqual(original,self.store.history(self.a,branch['id']))

    def test_independent_conversation_starts_empty_and_workspace_recall_is_explicit(self):
        user,reply=self.turn('CEDAR_BOUNDARY 使用 SQLite 保存原文','选择 BGE-M3 和 Chroma')
        independent=self.store.create_conversation(self.a,'独立实验')
        self.assertIsNone(independent['parent_id'])
        self.assertEqual(self.store.history(self.a,independent['id']),[])
        self.assertEqual(self.retriever.retrieve(self.a,'CEDAR_BOUNDARY',corpus='history',conversation_id=independent['id'])['results'],[])
        hit=self.retriever.retrieve(self.a,'CEDAR_BOUNDARY',corpus='history',conversation_id=independent['id'],scope='workspace')['results'][0]
        self.assertTrue(hit['cross_session']);self.assertFalse(hit['cross_workspace'])
        self.assertEqual(hit['source_conversation_id'],self.ca)
        self.assertEqual(hit['source_conversation_title'],'主线')
        self.assertIn('会话「主线」',hit['title'])
        with self.assertRaises(ValueError):self.retriever.read(self.a,hit['ref_id'],conversation_id=independent['id'])
        read=self.retriever.read(self.a,hit['ref_id'],conversation_id=independent['id'],allow_workspace=True)
        self.assertIn('SQLite',read['content'])
        self.assertEqual(read['source_conversation_title'],'主线')
        cross=self.retriever.retrieve(self.b,'CEDAR_BOUNDARY',corpus='history',conversation_id=self.cb,scope='all_sessions')['results'][0]
        with self.assertRaises(ValueError):self.retriever.read(self.b,cross['ref_id'],conversation_id=self.cb,allow_workspace=True)
        self.assertEqual(self.retriever.retrieve(self.b,'CEDAR_BOUNDARY',corpus='history',conversation_id=self.cb,scope='workspace')['results'],[])

    def test_archive_conversation_keeps_records_and_does_not_archive_workspace(self):
        self.turn('ARCHIVE_TOKEN','keep this')
        other=self.store.create_conversation(self.a,'另一会话')['id']
        job=self.store.enqueue(self.a,other,'work','work',[])
        self.store.claim_next()
        self.sessions.update(self.a,self.ca,{'archived':True})
        self.assertIsNone(self.store.space(self.a)['archived_at'])
        self.assertEqual(self.store.job(self.a,job['id'])['status'],'running')
        self.assertTrue(self.retriever.retrieve(self.a,'ARCHIVE_TOKEN',corpus='history',conversation_id=other,scope='workspace')['results'])
        with self.assertRaises(Conflict):self.sessions.update(self.a,other,{'archived':True})
        self.sessions.restore_branch(self.a,self.ca)
        self.assertEqual(len(self.store.history(self.a,self.ca)),2)

    def test_memory_scopes_and_ui_edits_do_not_leak_session_instructions(self):
        sibling=self.store.create_conversation(self.a,'对照组')['id']
        ids=[]
        for content in ['记住：SESSION_ONLY','研究区记住：WORKSPACE_ONLY','全局记住：GLOBAL_ONLY']:
            message=self.store.message(self.a,self.ca,'user',content)
            ids.append(self.memory.explicit(self.a,message))
        self.assertEqual({m['id'] for m in self.memory.snapshot(self.a,sibling)},set(ids[1:]))
        self.assertEqual({m['id'] for m in self.memory.snapshot(self.b,self.cb)},{ids[2]})
        self.memory.update(self.a,ids[0],{'content':'edited session preference'})
        self.assertNotIn(ids[0],{m['id'] for m in self.memory.snapshot(self.a,sibling)})
        self.memory.update(self.a,ids[0],{'scope':'workspace'})
        self.assertIn(ids[0],{m['id'] for m in self.memory.snapshot(self.a,sibling)})
        self.assertNotIn(ids[0],{m['id'] for m in self.memory.snapshot(self.b,self.cb)})

    def test_identical_preferences_in_independent_conversations_keep_provenance(self):
        sibling=self.store.create_conversation(self.a,'另一会话')['id']
        first=self.memory.explicit(self.a,self.store.message(self.a,self.ca,'user','记住：先给结论'))
        second=self.memory.explicit(self.a,self.store.message(self.a,sibling,'user','记住：先给结论'))
        self.assertNotEqual(first,second)
        self.assertEqual([m['id'] for m in self.memory.snapshot(self.a,sibling)],[second])

    def test_retry_remains_in_original_turn_after_newer_questions(self):
        first,_=self.turn('first','first reply')
        job=self.store.enqueue(self.a,self.ca,'first','first',[])
        self.store.cancel(self.a,job['id'])
        self.turn('later question','later answer')
        retry=self.store.retry(self.a,job['id'])
        self.assertEqual(retry['parent_message_id'],first['id'])
        self.assertEqual(retry['turn_seq'],first['turn_seq'])
        self.assertNotEqual(retry['id'],job['id'])
        self.assertEqual(retry['retry_of'],job['id'])
        with closing(self.store._connect()) as db:
            pending=db.execute('SELECT turn_seq FROM messages WHERE job_id=?',(retry['id'],)).fetchone()
        self.assertEqual(pending[0],first['turn_seq'])

    def test_only_explicit_global_preferences_cross_sessions_automatically(self):
        local=self.store.message(self.a,self.ca,'user','记住：本区使用 LOCALPREF')
        global_=self.store.message(self.a,self.ca,'user','全局记住：先给 GLOBALPREF 结论')
        self.memory.explicit(self.a,local);key=self.memory.explicit(self.a,global_)
        snapshot=self.memory.snapshot(self.b,self.cb)
        self.assertEqual([r['id'] for r in snapshot],[key])
        self.assertEqual(self.retriever.retrieve(self.b,'LOCALPREF',corpus='memory',scope='all_sessions')['results'],[])
        hit=self.retriever.retrieve(self.b,'GLOBALPREF',corpus='memory',scope='all_sessions')['results'][0]
        with self.assertRaises(ValueError):self.retriever.read(self.b,hit['ref_id'])
        self.assertIn('GLOBALPREF',self.retriever.read(self.b,hit['ref_id'],allow_cross=True)['content'])
        self.memory.update(self.a,key,{'scope':'session'})
        self.assertEqual(self.memory.snapshot(self.b,self.cb),[])
        with self.assertRaises(ValueError):self.retriever.read(self.b,hit['ref_id'],allow_cross=True)

    def test_cross_document_reads_keep_original_area_and_page(self):
        self.lib.save(self.a,{'kind':'paper','title':'机制来源','url':'','canonical_id':'paper1','metadata':{},'data':None,
            'chunks':[{'text':'AUTOMEM combines selection and feedback across tasks.','page':7,'section':'method','line_start':1,'line_end':2}],
            'warnings':[],'boundary':'fixture'})
        self.assertEqual(self.retriever.retrieve(self.b,'AUTOMEM')['results'],[])
        hit=self.retriever.retrieve(self.b,'AUTOMEM',scope='all_sessions')['results'][0]
        source=self.retriever.read(self.b,hit['ref_id'],allow_cross=True)
        self.assertEqual(source['space_id'],self.a);self.assertEqual(source['page'],7)
        self.assertIn('/'+self.a+'/',source['url'])

    def test_http_stop_is_scoped_idempotent_and_turn_recovery_works(self):
        server=make_server(port=0,db_path=self.store.path,trace_dir=self.root/'traces',start_worker=False)
        worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
        def api(path,method='GET',body=None):
            data=json.dumps(body).encode() if body is not None else None
            with urlopen(Request(f'http://127.0.0.1:{server.server_port}'+path,data=data,method=method,headers={'Content-Type':'application/json'}),timeout=5) as response:return json.load(response)
        try:
            user,_=self.turn();one=self.store.enqueue(self.a,self.ca,'one','one',[])
            two=self.store.enqueue(self.b,self.cb,'two','two',[])
            prefix=f'/api/spaces/{self.a}/conversations/{self.ca}'
            self.assertEqual(api(prefix+'/stop','POST',{})['stopped'],[one['id']])
            self.assertEqual(api(prefix+'/stop','POST',{})['stopped'],[])
            self.assertEqual(self.store.job(self.b,two['id'])['status'],'queued')
            api(prefix+'/messages/'+str(user['id']),'DELETE',{})
            self.assertEqual(api(prefix+'/messages')['messages'],[])
            self.assertEqual(len(api(f'/api/spaces/{self.a}/recycle')['turns']),1)
            api(prefix+'/turns/1/restore','POST',{})
            self.assertTrue(api(prefix+'/messages')['messages'])
        finally:
            server.app.close();server.shutdown();server.server_close();worker.join(3)


if __name__=='__main__':unittest.main()
