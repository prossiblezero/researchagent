"""Regressions for scoped search, live events and local folder selection."""
import json
import os
import subprocess
import tempfile
import threading
import unittest
from contextlib import closing
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from research_agent import ModelDecision, ResearchAgent, SearchResponse, TavilySearch
from research_agent.folders import choose_folder
from research_agent.search import ArxivSearch
from research_agent.workbench import OfflineRouter, Workbench, route_intent
from research_agent.workbench_store import WorkbenchStore
from server import Handler, make_server


class ScopedSearchTests(unittest.TestCase):
    def test_arxiv_enforces_host_and_overrides_query_scope(self):
        urls = ['https://arxiv.org/abs/2401.00001', 'https://export.arxiv.org/abs/2401.00002',
                'https://arxiv.org.evil.test/paper', 'https://notarxiv.org/a',
                'https://arxiv.org@evil.test/a', 'https://127.0.0.1/a', 'file:///arxiv.org/a',
                'https://arxiv.org/', 'https://info.arxiv.org/help/api/index.html']
        provider = Mock()
        provider.search.return_value = SearchResponse(True, 'original', [{'url': u} for u in urls], network_requests=2)
        response = ArxivSearch(provider).search('agent SITE:evil.test')
        self.assertEqual([r['url'] for r in response.results], urls[:2])
        self.assertEqual(provider.search.call_args.args, ('site:arxiv.org agent',))
        self.assertEqual(response.network_requests, 2)

    def test_arxiv_empty_and_failure_are_not_fabricated(self):
        provider = Mock()
        provider.search.return_value = SearchResponse(True, 'q', [{'url':'https://example.com'}])
        self.assertEqual(ArxivSearch(provider).search('q').results, [])
        failed = SearchResponse(False, 'q', error={'code':'search_transient'})
        provider.search.return_value = failed
        self.assertIs(ArxivSearch(provider).search('q'), failed)

    def test_tavily_receives_model_selected_domain_filters(self):
        for query, expected in [('site:arxiv.org agent', ['arxiv.org']), ('agent', None),
                                ('site:dblp.org OR site:aclanthology.org Agent', ['dblp.org','aclanthology.org']),
                                ('Agent (site:dblp.org OR site:aclanthology.org)', ['dblp.org','aclanthology.org']),
                                ('site:proceedings.mlr.press agent', ['proceedings.mlr.press']),
                                ('site:scholar.google.com memory', ['scholar.google.com']),
                                ('site:127.0.0.1 agent', None), ('agent -site:arxiv.org', None)]:
            with patch('research_agent.search.urlopen', return_value=BytesIO(b'{"results":[]}')) as call:
                TavilySearch('test-key').search(query)
            payload = json.loads(call.call_args.args[0].data)
            self.assertEqual(payload.get('include_domains'), expected)
            self.assertEqual(payload['query'], query)


class WorkbenchRefinementTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.store = WorkbenchStore(self.root / 'test.db')
        self.space = self.store.save_space({'name':'Scope test'})['id']
        self.conversation = self.store.create_conversation(self.space, 'Scope test')['id']

    def test_scope_survives_queue_execution_and_retry(self):
        captured = []
        agent = SimpleNamespace(model=OfflineRouter(),search=Mock(), run=lambda brief: captured.append(brief))
        app = Workbench(self.store, OfflineRouter, lambda observer: agent, self.root / 'traces', start_worker=False)
        self.addCleanup(app.close)
        queued = app.send(self.space, self.conversation, '研究 Agent 论文', 'arxiv')['job']
        self.assertEqual(queued['search_scope'], 'arxiv')
        app.execute(self.store.claim_next())
        self.assertIsInstance(agent.search, ArxivSearch)
        self.assertIn('仅 arxiv.org', captured[0])
        self.assertIn('read', captured[0])
        retry = app.retry(self.space, queued['id'])
        self.assertEqual(retry['search_scope'], 'arxiv')

    def test_invalid_scope_does_not_write_a_message(self):
        app = Workbench(self.store, OfflineRouter, Mock(), self.root / 'traces', start_worker=False)
        self.addCleanup(app.close)
        for scope in ('invalid', None, []):
            with self.assertRaises(ValueError):
                app.send(self.space, self.conversation, 'research', scope)
        self.assertEqual(self.store.history(self.space, self.conversation), [])

    def test_existing_v1_database_gains_default_without_losing_records(self):
        job = self.store.enqueue(self.space, self.conversation, 'old question', 'brief', [])
        with closing(self.store._connect()) as db, db:
            db.execute('ALTER TABLE research_jobs DROP COLUMN search_scope')
            db.execute('ALTER TABLE research_jobs DROP COLUMN research_effort')
        migrated = WorkbenchStore(self.store.path)
        self.assertEqual(migrated.job(self.space, job['id'])['search_scope'], 'web')
        self.assertEqual(migrated.job(self.space, job['id'])['research_effort'], 'legacy')
        self.assertEqual(migrated.job(self.space, job['id'])['question'], 'old question')

    def test_model_selects_effort_and_defaults_to_automatic_sources(self):
        for effort, limit in [('quick', 8), ('standard', 16), ('deep', 24)]:
            captured = []
            agent = SimpleNamespace(model=OfflineRouter(),search=Mock(), run=lambda brief: captured.append(brief))
            router = Mock()
            router.complete.return_value = ModelDecision('final', content=json.dumps({'intent':'RESEARCH','reply':'','brief':'按问题核查论文集和学术索引','assumptions':[],'effort':effort}))
            app = Workbench(self.store, lambda:router, lambda observer:agent, self.root/'traces', start_worker=False)
            try:
                job = app.send(self.space,self.conversation,'research')['job']
                self.assertEqual(job['search_scope'],'auto')
                self.assertEqual(job['research_effort'],effort)
                app.execute(self.store.claim_next())
                self.assertEqual(agent.max_tool_calls,limit)
                self.assertEqual(agent.max_rounds,limit+4)
                self.assertNotIsInstance(agent.search,ArxivSearch)
                self.assertIn('dblp.org',captured[0])
                self.assertIn('ccf.org.cn',captured[0])
                retried = app.retry(self.space,job['id'])
                self.assertEqual(retried['research_effort'],effort)
                self.assertEqual(retried['search_scope'],'auto')
            finally:
                app.close()

    def test_invalid_model_budget_cannot_enqueue_unbounded_research(self):
        model = Mock()
        for effort in (999, 'unlimited', 'legacy', None, [], 'none'):
            model.complete.return_value = ModelDecision('final',content=json.dumps({'intent':'RESEARCH','reply':'','brief':'b','assumptions':[],'effort':effort}))
            with self.assertRaises(ValueError):
                route_intent(model,[{'role':'user','content':'research'}],self.store.space(self.space))
        self.assertEqual(self.store.jobs(self.space),[])

    def test_automatic_research_can_pass_six_calls_and_finish_early(self):
        class MoreThanSix:
            name = 'seven-step-regression'
            count = 0
            def complete(model,messages,tools):
                model.count += 1
                if model.count <= 7:
                    return ModelDecision('tool_call',query='research source '+str(model.count),call_id=str(model.count))
                return ModelDecision('final',content='Agents use tools. [S1] [E1]')
        search = Mock()
        search.search.return_value = SearchResponse(True,'q',[{'url':'https://dblp.org/rec/example','title':'Agents use tools','snippet':'Agents use tools.'}])
        def factory(observer):
            return ResearchAgent(search,MoreThanSix(),self.root/'traces',max_tool_calls=1,on_event=observer)
        app = Workbench(self.store,OfflineRouter,factory,self.root/'traces',start_worker=False)
        self.addCleanup(app.close)
        task = app.send(self.space,self.conversation,'研究 Agent')['task_id']
        app.execute(self.store.claim_next())
        job = self.store.job(self.space,task)
        self.assertEqual(job['status'],'completed',job['error'])
        run = self.store.get(job['run_id'])
        self.assertEqual(run['tool_calls'],7)
        first = json.loads(Path(job['trace_path']).read_text(encoding='utf-8').splitlines()[0])
        self.assertEqual(first['config']['max_tool_calls'],16)
        self.assertEqual(first['config']['max_rounds'],20)

    def test_deep_research_retains_evidence_beyond_old_context_ceiling(self):
        class ResearchModel:
            name = 'many-sources-regression'
            count = 0
            def complete(model,messages,tools):
                model.count += 1
                if model.count <= 14:
                    return ModelDecision('tool_call',query='source group '+str(model.count),call_id=str(model.count))
                return ModelDecision('final',content='Agents use tools. [S1] [E1]')
        class ManySources:
            count = 0
            def search(provider,query):
                provider.count += 1
                return SearchResponse(True,query,[{'url':f'https://dblp.org/rec/{provider.count}/{i}', 'title':'Agents use tools '+('research '*20), 'snippet':'Agents use tools. '+('Evidence about agent memory. '*40)} for i in range(5)])
        router = Mock()
        router.complete.return_value = ModelDecision('final',content=json.dumps({'intent':'RESEARCH','reply':'','brief':'比较多个论文方向','assumptions':[],'effort':'deep'}))
        app = Workbench(self.store,lambda:router,lambda observe:ResearchAgent(ManySources(),ResearchModel(),self.root/'traces',on_event=observe),self.root/'traces',start_worker=False)
        self.addCleanup(app.close)
        task=app.send(self.space,self.conversation,'研究多个领域')['task_id']
        app.execute(self.store.claim_next())
        job=self.store.job(self.space,task)
        self.assertEqual(job['status'],'completed',job['error'])
        run=self.store.get(job['run_id'])
        self.assertEqual(run['tool_calls'],14)
        self.assertEqual(len(run['sources']),70)
        events=[json.loads(line) for line in Path(job['trace_path']).read_text(encoding='utf-8').splitlines()]
        self.assertEqual(events[0]['config']['max_context_tokens'],65536)
        self.assertFalse(any(e.get('error')=='context_budget_exceeded' for e in events))


class RefinementHttpTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.server = make_server(port=0, db_path=self.root / 'app.db', trace_dir=self.root / 'traces', start_worker=False, model_factory=OfflineRouter)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        def close():
            self.server.shutdown()
            self.server.server_close()
            thread.join(2)
        self.addCleanup(close)
        self.address = 'http://127.0.0.1:' + str(self.server.server_port)
        store = self.server.app.store
        self.a = store.save_space({'name':'A'})['id']
        self.b = store.save_space({'name':'B'})['id']
        self.c = store.create_conversation(self.a, 'HTTP test')['id']
        self.job = store.enqueue(self.a, self.c, 'question', 'brief', [])
        self.prefix = '/api/spaces/' + self.a + '/jobs/' + self.job['id']

    def request(self, path, data=None, headers=None):
        req = Request(self.address + path, data=json.dumps(data).encode() if data is not None else None,
                      headers={'Content-Type':'application/json', **(headers or {})})
        with urlopen(req, timeout=3) as response:
            return json.loads(response.read())

    def test_live_events_cursor_partial_tail_and_space_boundary(self):
        self.assertEqual(self.request(self.prefix + '/events')['events'], [])
        job = self.server.app.store.claim_next()
        path = self.root / 'traces' / 'live.jsonl'
        path.parent.mkdir()
        path.write_text(''.join(json.dumps({'seq': n, 'event':'test'}) + '\n' for n in range(1, 203)) + '{"seq":203', encoding='utf-8')
        self.server.app.store.progress(job['id'], 'search', 'working', str(path))
        first = self.request(self.prefix + '/events')
        self.assertEqual(len(first['events']), 200)
        self.assertEqual(first['status'], 'running')
        second = self.request(self.prefix + '/events?after=200')
        self.assertEqual([e['seq'] for e in second['events']], [201, 202])
        self.assertEqual(self.request(self.prefix + '/events?after=202')['next_seq'], 202)
        with path.open('a', encoding='utf-8') as stream:
            stream.write(',"event":"done"}\n')
        self.assertEqual(self.request(self.prefix + '/events?after=202')['next_seq'], 203)
        for route, expected in [(self.prefix.replace(self.a, self.b) + '/events', 404), (self.prefix + '/events?after=-1', 400), (self.prefix + '/events?after=x', 400)]:
            with self.assertRaises(HTTPError) as error:
                self.request(route)
            self.assertEqual(error.exception.code, expected)

    def test_events_refuses_trace_path_escape(self):
        job = self.server.app.store.claim_next()
        outside = self.root / 'outside.jsonl'
        outside.write_text('{"seq":1}\n')
        self.server.app.store.progress(job['id'], 'search', 'working', str(outside))
        with self.assertRaises(HTTPError) as error:
            self.request(self.prefix + '/events')
        self.assertEqual(error.exception.code, 404)

    def test_http_scope_is_stored_and_invalid_scope_rejected(self):
        route = '/api/spaces/' + self.a + '/conversations/' + self.c + '/messages'
        result = self.request(route, {'content':'研究 Agent', 'search_scope':'arxiv'})
        self.assertEqual(result['job']['search_scope'], 'arxiv')
        automatic = self.request(route, {'content':'研究 Agent'})
        self.assertEqual(automatic['job']['search_scope'], 'auto')
        self.assertEqual(automatic['job']['research_effort'], 'standard')
        with self.assertRaises(HTTPError) as error:
            self.request(route, {'content':'research', 'search_scope':{}})
        self.assertEqual(error.exception.code, 400)

    def test_folder_endpoint_requires_local_same_origin_and_handles_cancel(self):
        with patch.object(Handler, 'local_desktop', return_value=True), patch('server.choose_folder') as picker:
            picker.return_value = {'path':None, 'cancelled':True}
            self.assertTrue(self.request('/api/folders/pick', {}, {'Origin':self.address})['cancelled'])
            picker.return_value = {'path':str(self.root), 'cancelled':False}
            self.assertEqual(self.request('/api/folders/pick', {'initial':str(self.root)}, {'Origin':self.address})['path'], str(self.root))
            for headers in ({}, {'Origin':'https://evil.test'}, {'Origin':self.address, 'Sec-Fetch-Site':'cross-site'}):
                with self.assertRaises(HTTPError) as error:
                    self.request('/api/folders/pick', {}, headers)
                self.assertEqual(error.exception.code, 403)
            self.assertEqual(picker.call_count, 2)
        with patch.object(Handler, 'local_desktop', return_value=False), patch('server.choose_folder') as picker:
            with self.assertRaises(HTTPError):
                self.request('/api/folders/pick', {}, {'Origin':self.address})
            picker.assert_not_called()

    def test_folder_capability_rejects_remote_hosts_and_public_bind(self):
        handler = SimpleNamespace(headers={'Host':'localhost:8000'}, client_address=('127.0.0.1', 1), server=SimpleNamespace(server_address=('127.0.0.1',8000)))
        self.assertEqual(Handler.local_desktop(handler), os.name == 'nt')
        handler.headers['Host'] = 'evil.test:8000'
        self.assertFalse(Handler.local_desktop(handler))
        handler.headers['Host'] = 'localhost:8000'
        handler.server.server_address = ('0.0.0.0',8000)
        self.assertFalse(Handler.local_desktop(handler))

    @unittest.skipUnless(os.name == 'nt', 'Windows native chooser')
    def test_native_picker_success_cancel_timeout_and_invalid_paths(self):
        for selected in (str(self.root), None):
            with patch('research_agent.folders.subprocess.run', return_value=SimpleNamespace(returncode=0, stdout=json.dumps({'path':selected}))) as process:
                result = choose_folder(str(self.root))
            self.assertEqual(result['cancelled'], selected is None)
            self.assertNotIn('shell', process.call_args.kwargs)
        with patch('research_agent.folders.subprocess.run', side_effect=subprocess.TimeoutExpired('chooser', 180)):
            with self.assertRaisesRegex(ValueError, '超时'):
                choose_folder()
        for path in ('../outside', '\\\\host\\share'):
            with self.assertRaises(ValueError):
                choose_folder(path)


if __name__ == '__main__':
    unittest.main()
