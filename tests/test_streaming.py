"""Real incremental transport boundaries; no external models or paid calls."""
import json
import tempfile
import threading
import unittest
from contextlib import closing
from io import BytesIO
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from research_agent.models import OpenAICompatibleModel
from research_agent.streaming import public_text, read_chat_stream, reply_prefix
from research_agent.workbench import OfflineRouter, Workbench
from research_agent.workbench_store import WorkbenchStore
from server import make_server


def frame(delta=None, finish=None, usage=None):
    data={'choices':[{'index':0,'delta':delta or {},'finish_reason':finish}]}
    if usage is not None:
        data={'choices':[],'usage':usage}
    return ('data: '+json.dumps(data,ensure_ascii=False)+'\n\n').encode()


class StreamTests(unittest.TestCase):
    def test_heartbeat_only_stream_checks_request_guard(self):
        class Heartbeats(BytesIO):
            lines = 0
            def readline(self, *args):
                self.lines += 1
                return super().readline(*args)
        response = Heartbeats(b': keepalive\n\n'*20 + frame({'content':'too late'}, 'stop'))
        model = OpenAICompatibleModel('https://example.com', 'test-key', 'test')
        model.stream_callback = lambda event: None
        def guard():
            if response.lines >= 2:
                raise RuntimeError('cancelled during heartbeat')
        model.request_guard = guard
        with patch('research_agent.models.urlopen', return_value=response), \
                self.assertRaisesRegex(RuntimeError, 'cancelled during heartbeat'):
            model.complete([], [])
        self.assertLessEqual(response.lines, 2)

    def test_tool_fragments_usage_and_reasoning_are_preserved_without_public_reasoning(self):
        response=BytesIO(frame({'reasoning_content':'private chain'})+frame({'tool_calls':[{'index':0,'id':'c1','function':{'name':'se','arguments':'{"que'}}]})+
            frame({'tool_calls':[{'index':0,'function':{'name':'arch','arguments':'ry":"中文"}'}}]},'tool_calls')+
            frame(usage={'prompt_tokens':12,'completion_tokens':5,'total_tokens':17})+b'data: [DONE]\n\n')
        events=[];model=OpenAICompatibleModel('https://example.com/v1','test-key','test')
        model.stream_callback=events.append
        with patch('research_agent.models.urlopen',return_value=response) as transport:
            decision=model.complete([{'role':'user','content':'test'}],[])
        self.assertEqual((decision.tool_name,decision.query,decision.call_id),('search','中文','c1'))
        self.assertEqual(decision.reasoning_content,'private chain')
        self.assertNotIn('private chain',json.dumps(events))
        self.assertEqual(model.total_usage['total_tokens'],17)
        self.assertTrue(json.loads(transport.call_args.args[0].data)['stream'])
        self.assertEqual(events[-1]['phase'],'end')

    def test_truncated_stream_retries_without_executing_partial_tool(self):
        events=[];model=OpenAICompatibleModel('https://example.com','test-key','test');model.stream_callback=events.append
        broken=BytesIO(frame({'tool_calls':[{'index':0,'function':{'name':'read','arguments':'{"url":'}}]}))
        good=BytesIO(frame({'content':'恢复成功。'},'stop')+b'data: [DONE]\n\n')
        with patch('research_agent.models.urlopen',side_effect=[broken,good]),patch('research_agent.models.time.sleep'):
            result=model.complete([],[])
        self.assertEqual(result.kind,'final');self.assertEqual(result.content,'恢复成功。')
        self.assertIn('retry',[e['phase'] for e in events]);self.assertEqual(len(model.usage_records),2)
        self.assertNotEqual(events[0]['request_id'],events[-1]['request_id'])
        with self.assertRaises(ConnectionError):
            read_chat_stream(BytesIO(frame({'content':'不完整'})),lambda *a:None)

    def test_json_gateway_fallback_and_callback_cancellation(self):
        model=OpenAICompatibleModel('https://example.com','test-key','test');events=[];model.stream_callback=events.append
        with patch('research_agent.models.urlopen',return_value=BytesIO(b'{"choices":[{"message":{"content":"hello"}}]}')):
            self.assertEqual(model.complete([],[]).content,'hello')
        self.assertFalse(next(e for e in events if e['phase']=='update')['streaming'])
        class Cancelled(Exception):pass
        def cancelled(record):raise Cancelled()
        model.stream_callback=cancelled
        with patch('research_agent.models.urlopen') as transport,self.assertRaises(Cancelled):model.complete([],[])
        transport.assert_not_called()

    def test_in_band_overload_is_retried_but_authentication_error_is_not(self):
        for kind,attempts in [('upstream_error',3),('authentication_error',1)]:
            model=OpenAICompatibleModel('https://example.com','test-key','test');events=[];model.stream_callback=events.append
            def response(*args,**kwargs):return BytesIO(('data: '+json.dumps({'error':{'type':kind,'message':'provider failed'}})+'\n\n').encode())
            with patch('research_agent.models.urlopen',side_effect=response) as transport,patch('research_agent.models.time.sleep'),self.assertRaises(RuntimeError):
                model.complete([],[])
            self.assertEqual(transport.call_count,attempts)
            self.assertEqual(events[-1]['phase'],'error')

    def test_partial_json_reply_and_secrets(self):
        prefix='{"intent":"CHAT","reply":"你好\\n引用\\u4e2'
        self.assertEqual(reply_prefix(prefix),'你好\n引用')
        self.assertEqual(reply_prefix(prefix+'d\\"内容\\""}'),'你好\n引用中"内容"')
        self.assertEqual(public_text('secret reasoning','answer_verification',final=True),'')
        self.assertEqual(public_text('hello API_KEY=sk-abcd','answer'),'hello ')
        self.assertNotIn('sk-abcdefghijklm',public_text('key sk-abcdefghijklm ','answer',secret='sk-abcdefghijklm'))

    def test_background_send_does_not_wait_for_router_and_keeps_local_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=WorkbenchStore(Path(tmp)/'app.db');space=store.save_space({'name':'test'})['id'];conv=store.create_conversation(space,'test')['id']
            app=Workbench(store,OfflineRouter,lambda _:None,Path(tmp)/'traces',start_worker=False)
            try:
                with patch('research_agent.workbench.route_intent',side_effect=AssertionError('must not block send')):
                    result=app.send(space,conv,'你好',background=True)
                with patch('research_agent.workbench.route_intent',return_value={'intent':'CHAT','reply':'你好','brief':'','assumptions':[],'effort':'none'}) as route:
                    app.execute(store.claim_next())
                self.assertIn('local_material_count',route.call_args.args[2])
                self.assertEqual(store.job(space,result['task_id'])['kind'],'CHAT')
                self.assertEqual(store.history(space,conv)[-1]['content'],'你好')
            finally:app.close()

    def test_http_delivers_draft_before_provider_finishes_and_reconnect_is_scoped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);release=threading.Event();first=threading.Event()
            class SlowStream(BytesIO):
                def readline(self,*args):
                    line=super().readline(*args)
                    if not line and not first.is_set():
                        first.set();release.wait(8)
                        self.seek(0);self.truncate();self.write(frame({'content':'继续。","brief":"","assumptions":[],"effort":"none"}'},'stop')+b'data: [DONE]\n\n');self.seek(0)
                        return super().readline(*args)
                    return line
            model=lambda:OpenAICompatibleModel('https://example.com','test-key','test')
            server=make_server(port=0,db_path=root/'app.db',trace_dir=root/'traces',model_factory=model,start_worker=False)
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            store=server.app.store;space=store.save_space({'name':'stream'})['id'];conv=store.create_conversation(space,'stream')['id']
            other=store.save_space({'name':'other'})['id'];address=f'http://127.0.0.1:{server.server_port}'
            path=f'/api/spaces/{space}/conversations/{conv}/events'
            try:
                for target in (path.replace(space,other),path+'?after=-1'):
                    with self.assertRaises(HTTPError):urlopen(address+target,timeout=3)
                job=server.app.send(space,conv,'你好',background=True)['job']
                source=SlowStream(frame({'content':'{"intent":"CHAT","reply":"先输出。'}))
                with patch('research_agent.models.urlopen',return_value=source):
                    server.app.worker.start()
                    with urlopen(address+path,timeout=10) as response:
                        seq=0;seen=False
                        for _ in range(120):
                            line=response.readline().decode()
                            if line.startswith('id:'):seq=int(line[3:])
                            if line.startswith('data:'):
                                data=json.loads(line[5:]);payload=data.get('payload',{})
                                if payload.get('text')=='先输出。':
                                    seen=True;break
                        self.assertTrue(seen,'No text arrived while model was still running')
                        self.assertFalse(release.is_set());self.assertEqual(store.job(space,job['id'])['status'],'running')
                    # Reconnect from a saved cursor; the next event must not replay older text.
                    with urlopen(Request(address+path,headers={'Last-Event-ID':str(seq)}),timeout=10) as response:
                        release.set()
                        for _ in range(150):
                            line=response.readline().decode()
                            if line.startswith('id:'):
                                self.assertGreater(int(line[3:]),seq);break
                        else:self.fail('No resumed event')
            finally:
                release.set();server.app.stop.set();server.shutdown();server.server_close();thread.join(3)


if __name__=='__main__':unittest.main()
