"""V1 acceptance: durable history, real harness integration, recovery and HTTP isolation."""
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from research_agent import FixtureReader, FixtureSearch, FailingSearch, ModelDecision, OfflineModel, ResearchAgent, RunResult, SearchResponse
from research_agent.workbench import OfflineRouter, Workbench, route_intent
from research_agent.workbench_store import Conflict, NotFound, WorkbenchStore
from server import make_server

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "fixtures" / "search_results.json"


class WorkbenchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = WorkbenchStore(self.root / "state.db")
        self.a = self.store.save_space({"name": "Agent"})["id"]
        self.b = self.store.save_space({"name": "RAG"})["id"]
        self.ca = self.store.create_conversation(self.a, "方向探索")["id"]
        self.cb = self.store.create_conversation(self.b, "资料整理")["id"]
        self.app = Workbench(self.store, OfflineRouter, self.agent, self.root / "traces", start_worker=False)
        self.addCleanup(self.app.close)

    def agent(self, observer, search=None, model=None):
        return ResearchAgent(search or FixtureSearch.from_file(FIXTURE), model or OfflineModel(), self.root / "traces", reader=FixtureReader.from_file(FIXTURE), on_event=observer)

    def submit(self):
        return self.app.send(self.a, self.ca, "我想研究一下 Agent")["task_id"]

    def test_spaces_crud_and_chat_never_starts_research(self):
        with patch.object(self.app, "agent_factory", side_effect=AssertionError("chat must not search")):
            reply = self.app.send(self.a, self.ca, "你好")
        self.assertEqual(reply["intent"], "CHAT")
        self.assertEqual(len(self.store.history(self.a, self.ca)), 2)
        self.assertEqual(self.store.history(self.b, self.cb), [])
        self.assertEqual(self.store.jobs(self.a), [])
        self.assertEqual(self.store.save_space({"name": "Agent 研究"}, self.a)["name"], "Agent 研究")
        self.store.delete_space(self.b)
        with self.assertRaises(NotFound):
            self.store.history(self.b, self.cb)

    def test_exploration_run_saves_report_history_and_trace(self):
        task = self.submit()
        self.assertEqual(self.store.job(self.a, task)["status"], "queued")
        self.app.execute(self.store.claim_next())
        job = self.store.job(self.a, task)
        self.assertEqual(job["status"], "completed", job["error"])
        self.assertIn("方向", job["brief"])
        self.assertTrue(json.loads(job["assumptions"]))
        self.assertTrue(Path(job["trace_path"]).is_file())
        self.assertIn("## 来源", self.app.report_file(self.a, task, "md").read_text(encoding="utf-8"))
        self.assertIn("<!doctype html>", self.app.report_file(self.a, task, "html").read_text(encoding="utf-8"))
        self.assertIn('<article>', self.app.report_file(self.a, task, "html").read_text(encoding="utf-8"))
        run = self.store.get(job["run_id"])
        self.assertTrue(run["evidence"])
        self.assertTrue(run["claims"])
        reopened = WorkbenchStore(self.store.path)
        self.assertEqual(reopened.job(self.a, task)["status"], "completed")
        self.assertEqual(reopened.history(self.a, self.ca)[-1]["content"], job["summary"])

    def test_scoped_ids_cannot_cross_spaces(self):
        task = self.submit()
        for operation in (
            lambda: self.store.history(self.b, self.ca),
            lambda: self.store.job(self.b, task),
            lambda: self.store.cancel(self.b, task),
            lambda: self.app.send(self.b, self.ca, "你好"),
            lambda: self.store.enqueue(self.b, self.ca, "q", "b", []),
        ):
            with self.assertRaises(NotFound):
                operation()
        self.assertEqual(self.store.history(self.b, self.cb), [])

    def test_resume_reuses_saved_context_without_session_compression(self):
        task=self.submit();self.app.execute(self.store.claim_next())
        # Simulate interruption after the final checkpoint but before commit.
        with closing(self.store._connect()) as db,db:
            db.execute("UPDATE research_jobs SET status='interrupted' WHERE id=?",(task,))
        self.app.resume(self.a,task)
        with patch.object(self.app.sessions,'prompt_context',side_effect=AssertionError('saved history must be reused')):
            self.app.execute(self.store.claim_next())
        job=self.store.job(self.a,task)
        self.assertEqual(job['status'],'completed',job['error'])
        events=[json.loads(line) for line in Path(job['trace_path']).read_text(encoding='utf-8').splitlines()]
        self.assertTrue(any(e['event']=='execution_resumed' for e in events))
        self.assertFalse(any(e['event']=='model_request' for e in events))

    def test_followup_can_research_again_then_answer_from_conversation(self):
        calls = []
        routes = iter([
            {"intent": "RESEARCH", "reply": "", "brief": "核实 Meta-Harness 的含义，简短解释并给来源", "assumptions": [], "effort": "quick"},
            {"intent": "RESEARCH", "reply": "", "brief": "核实上轮 Meta-Harness 项目的公开代码状态", "assumptions": [], "effort": "quick"},
            {"intent": "CHAT", "reply": "上面的结果说明了该项目的用途与公开代码情况。", "brief": "", "assumptions": [], "effort": "none"},
        ])
        class Router:
            def complete(self, messages, tools):
                calls.append(messages)
                return ModelDecision("final", content=json.dumps(next(routes)))
        self.app.model_factory = Router
        tasks = []
        for question in ("你知道 meta-harness 吗？", "它的代码开源了吗？"):
            task = self.app.send(self.a, self.ca, question)["task_id"]
            tasks.append(task)
            self.app.execute(self.store.claim_next())
            self.assertEqual(self.store.job(self.a, task)["status"], "completed")
        self.assertNotEqual(*tasks)
        previous_answer = self.store.job(self.a, tasks[0])["summary"]
        self.assertTrue(any(m["role"] == "assistant" and m["content"] == previous_answer for m in calls[1]))
        reply = self.app.send(self.a, self.ca, "把上面结果缩成一句话")
        self.assertEqual(reply["intent"], "CHAT")
        self.assertEqual(len(self.store.jobs(self.a)), 2)
        self.assertEqual(self.store.history(self.b, self.cb), [])

    def test_semantic_plan_and_original_question_reach_worker_and_retry(self):
        question = "找两篇让智能体跨任务利用过去经验的论文，比较保存和使用经验的机制"
        plan = "目标：跨任务经验复用。纳入：经验用于后续决策；排除仅记录日志。路径：experience reuse；feedback-guided policy improvement。最多两篇。"
        router = type('Router', (), {'complete': lambda model, messages, tools: ModelDecision('final', content=json.dumps({
            'intent':'RESEARCH','reply':'','brief':plan,'assumptions':[],'effort':'standard'}))})
        self.app.model_factory = router
        seen = []
        model = type('ResearchModel', (), {'name':'semantic-plan-fixture', 'complete': lambda model, messages, tools: (
            seen.append(messages) or ModelDecision('final', content='INSUFFICIENT：测试不提供外部资料。'))})
        self.app.agent_factory = lambda observer: self.agent(observer, model=model())
        task = self.app.send(self.a, self.ca, question)['task_id']
        self.assertEqual(self.store.job(self.a, task)['brief'], plan)
        self.app.execute(self.store.claim_next())
        retry = self.app.retry(self.a, task)
        self.app.execute(self.store.claim_next())
        self.assertEqual(self.store.job(self.a, retry['id'])['brief'], plan)
        for messages in seen:
            self.assertTrue(any(question in str(m.get('content','')) for m in messages))
            self.assertTrue(any(plan in str(m.get('content','')) for m in messages))

    def test_restart_interrupts_queued_and_running_without_erasing_runs(self):
        task = self.submit()
        self.store.claim_next()
        other = self.submit()
        restored = Workbench(WorkbenchStore(self.store.path), OfflineRouter, self.agent, self.root / "traces", start_worker=False)
        self.addCleanup(restored.close)
        self.assertEqual([self.store.job(self.a, j)["status"] for j in (task, other)], ["interrupted", "interrupted"])
        retry = restored.retry(self.a, task)
        self.assertNotEqual(retry["id"], task)
        self.assertEqual(retry["retry_of"], task)
        with self.assertRaises(Conflict):
            restored.retry(self.a, task)
        restored.execute(self.store.claim_next())
        self.assertEqual(self.store.job(self.a, retry["id"])["status"], "completed")

    def test_cancel_running_prevents_followup_tools_and_completion(self):
        task = self.submit()
        entered, release = threading.Event(), threading.Event()
        search = FixtureSearch.from_file(FIXTURE)
        original = search.search
        def slow_search(query):
            entered.set()
            release.wait(3)
            return original(query)
        search.search = slow_search
        self.app.agent_factory = lambda observer: self.agent(observer, search)
        runner = threading.Thread(target=self.app.execute, args=(self.store.claim_next(),))
        runner.start()
        try:
            self.assertTrue(entered.wait(2))
            self.store.cancel(self.a, task)
        finally:
            release.set()
            runner.join(3)
        self.assertFalse(runner.is_alive())
        job = self.store.job(self.a, task)
        self.assertEqual(job["status"], "cancelled")
        self.assertIsNone(job["report"])
        events = [json.loads(line) for line in Path(job["trace_path"]).read_text(encoding="utf-8").splitlines()]
        self.assertEqual([e["name"] for e in events if e["event"] == "tool_call_requested"], ["search"])
        self.assertFalse(self.store.finish(task, "completed", "late result"))

    def test_queue_cancel_and_active_space_deletion(self):
        task = self.submit()
        with self.assertRaises(Conflict):
            self.store.delete_space(self.a)
        self.store.cancel(self.a, task)
        self.assertIsNone(self.store.claim_next())
        self.store.delete_space(self.a)
        self.assertEqual(len(self.store.spaces()), 1)

    def test_failure_is_visible_retryable_and_does_not_kill_worker(self):
        self.app.agent_factory = lambda observer: self.agent(observer, FailingSearch())
        task = self.submit()
        self.app.execute(self.store.claim_next())
        job = self.store.job(self.a, task)
        self.assertEqual(job["status"], "failed")
        self.assertIn("INSUFFICIENT", job["summary"])
        self.assertIsNotNone(job["report"])
        self.app.agent_factory = self.agent
        retry = self.app.retry(self.a, task)
        self.app.execute(self.store.claim_next())
        self.assertEqual(self.store.job(self.a, retry["id"])["status"], "completed")

    def test_experiment_chat_does_not_authorize_execution(self):
        result = self.app.send(self.a, self.ca, '执行实验')
        self.assertEqual(result['intent'], 'EXPERIMENT')
        self.assertIn('不会自动执行', result['message']['content'])
        self.assertEqual(self.store.jobs(self.a), [])
        result=self.app.send(self.a,self.ca,'提出创新方案')
        self.assertEqual(result['job']['kind'],'BRAINSTORM')

    def test_llm_sees_bounded_conversation_and_rejects_invalid_output(self):
        calls = []
        class FakeModel:
            def complete(self, messages, tools):
                calls.append((messages, tools))
                return ModelDecision("final", content=json.dumps({"intent": "RESEARCH", "reply": "", "brief": "探索历史中讨论的 Agent Harness", "assumptions": [], "effort": "standard"}))
        self.store.message(self.a, self.ca, "user", "我关注 Agent Harness")
        self.app.model_factory = FakeModel
        self.app.send(self.a, self.ca, "研究一下这个方向")
        self.assertIn("我关注 Agent Harness", json.dumps(calls[0][0], ensure_ascii=False))
        self.assertEqual(calls[0][1], [])
        # Queued followers are routed only when scheduled; isolate invalid routing on an idle conversation.
        for job in self.store.jobs(self.a):
            self.store.cancel(self.a,job['id'])
        for content in ('{}', '[]', '{"intent":"SHELL"}', 'not json'):
            with patch.object(FakeModel, "complete", return_value=ModelDecision("final", content=content)):
                with self.assertRaises(ValueError):
                    self.app.send(self.a, self.ca, "你好")
        self.assertEqual(len(self.store.jobs(self.a)), 1)
        self.assertEqual(self.store.history(self.a, self.ca)[-1]["intent"], "ERROR")

    def test_thinking_provider_accepts_compacted_research_context(self):
        class ThinkingModel:
            name = "thinking-protocol-fixture"
            count = 0
            saw_compaction = False
            def complete(model, messages, tools):
                for message in messages:
                    if '"CONTEXT_STATE"' in str(message.get("content", "")):
                        model.saw_compaction = True
                    if message["role"] == "assistant" and "reasoning_content" not in message:
                        raise RuntimeError("reasoning_content must be passed back for assistant turns")
                model.count += 1
                if model.count <= 4:
                    return ModelDecision("tool_call", query="agent search " + str(model.count), call_id=str(model.count), reasoning_content="opaque provider reasoning " + str(model.count))
                return ModelDecision("final", content="The agent uses tools. [S1] [E1]")
        class LongSearch:
            def search(search, query):
                return SearchResponse(True, query, [{"title": query, "url": "https://example.org/" + query[-1], "snippet": "The agent uses tools. " * 90}])
        model = ThinkingModel()
        result = ResearchAgent(LongSearch(), model, self.root / "traces", max_tool_calls=4, max_context_tokens=4200, output_reserve_tokens=300, context_safety_margin_tokens=100).run("Research agent tool usage")
        self.assertTrue(model.saw_compaction)
        self.assertEqual(result.termination, "model_final")
        self.assertEqual(result.status, "ok")

    def test_budget_handoff_explicitly_requests_a_cited_report(self):
        class BudgetModel:
            name = "budget-handoff-fixture"
            def complete(model, messages, tools):
                if tools:
                    return ModelDecision("tool_call", query="Agent", call_id="first")
                # Reproduce providers emitting textual tool calls when schemas simply disappear.
                if not any("FINAL_ONLY" in str(m.get("content","")) for m in messages):
                    return ModelDecision("final", content='<invoke name="search">more sources</invoke>')
                return ModelDecision("final", content="This tutorial builds an agent incrementally. [S1] [E1]")
        result = ResearchAgent(FixtureSearch.from_file(FIXTURE), BudgetModel(), self.root / "traces", max_tool_calls=1).run("Research Agent tutorials")
        self.assertEqual(result.tool_calls, 1)
        self.assertEqual(result.status, "ok")
        self.assertIn("[S1]", result.answer)

    def test_path_validation_report_escape_and_boundary(self):
        for path in ("../outside", str(self.root / ".." / "outside"), "\\\\host\\share"):
            with self.assertRaises(ValueError):
                self.store.save_space({"download_root": path}, self.a)
        task = self.submit()
        job = self.store.claim_next()
        result = RunResult('<script>alert(1)</script>', [], str(self.root / 'x.jsonl'), 'insufficient', 'model_final', 0, [], [])
        files = self.app._report(job, result, "x")
        self.assertIn('&lt;script&gt;', Path(files[1]).read_text(encoding='utf-8'))
        self.assertNotIn('<script>', Path(files[1]).read_text(encoding='utf-8'))
        self.app.execute(job)
        with closing(self.store._connect()) as db, db:
            db.execute("UPDATE reports SET markdown_path=? WHERE job_id=?", (str(self.root / "secret.md"), task))
        with self.assertRaises(ValueError):
            self.app.report_file(self.a, task, "md")

    def test_additive_migration_keeps_pre_h3_evidence_and_run(self):
        path = self.root / "legacy.db"
        with closing(sqlite3.connect(path)) as db, db:
            db.executescript("""CREATE TABLE runs (id TEXT PRIMARY KEY, question TEXT NOT NULL, answer TEXT NOT NULL, status TEXT NOT NULL, termination TEXT NOT NULL, tool_calls INTEGER NOT NULL, trace_path TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE evidence (id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, evidence_id TEXT NOT NULL, source_id TEXT NOT NULL, content TEXT NOT NULL, kind TEXT NOT NULL, retrieved_at TEXT NOT NULL);
            INSERT INTO runs VALUES ('legacy','old question','old answer','ok','model_final',1,'old.jsonl','2026-01-01');
            INSERT INTO evidence VALUES (1,'legacy','E1','S1','original evidence','snippet','2026-01-01');""")
        migrated = WorkbenchStore(path)
        self.assertEqual(migrated.get("legacy")["evidence"][0]["content"], "original evidence")
        self.assertEqual(WorkbenchStore(path).get("legacy")["answer"], "old answer")


class HttpAcceptanceTests(unittest.TestCase):
    def test_evaluation_reader_is_confined_to_published_report_directory(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict('os.environ',{'OFFLINE_MODE':'1'}):
            root=Path(tmp);published=root/'evals/reports/context-memory-optimization-20260918';published.mkdir(parents=True)
            (published/'report.html').write_text('<h1>Readable results</h1>',encoding='utf-8')
            (root/'private.json').write_text('{"private":true}',encoding='utf-8')
            with patch('server.ROOT',root):
                server=make_server(port=0,db_path=root/'state.db',trace_dir=root/'traces',start_worker=False)
                thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
                address='http://127.0.0.1:'+str(server.server_port)
                try:
                    with urlopen(address+'/evaluation/report.html',timeout=5) as response:
                        self.assertEqual(response.headers.get_content_type(),'text/html')
                        self.assertIn(b'Readable results',response.read())
                    for path in ('/evaluation/%2e%2e/%2e%2e/%2e%2e/private.json','/evaluation/state.db','/evaluation/missing.html'):
                        with self.assertRaises(HTTPError) as denied:urlopen(address+path,timeout=5)
                        self.assertEqual(denied.exception.code,404)
                finally:
                    server.shutdown();server.server_close();thread.join(2)

    def test_api_lifecycle_and_disconnected_client(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict("os.environ", {"OFFLINE_MODE": "1"}):
            root = Path(tmp)
            server = make_server(port=0, db_path=root / "app.db", trace_dir=root / "traces")
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            address = "http://127.0.0.1:" + str(server.server_port)
            def request(path, method="GET", data=None, headers=None):
                req = Request(address + path, data=json.dumps(data).encode() if data is not None else None, method=method, headers={"Content-Type": "application/json", **(headers or {})})
                with urlopen(req, timeout=5) as response:
                    raw = response.read()
                    return response.status, json.loads(raw) if response.headers.get_content_type() == "application/json" else raw
            try:
                self.assertIn(b"ResearchAgent", request("/")[1])
                self.assertEqual(request("/health")[1]["version"], "v4")
                a = request("/api/spaces", "POST", {"name": "Agent"})[1]["id"]
                b = request("/api/spaces", "POST", {"name": "RAG"})[1]["id"]
                prefix = "/api/spaces/" + a
                c = request(prefix + "/conversations", "POST", {})[1]["id"]
                messages = prefix + "/conversations/" + c + "/messages"
                self.assertEqual(request(messages, "POST", {"content": "你好"})[1]["intent"], "CHAT")
                status, response = request(messages, "POST", {"content": "我想研究一下 Agent"})
                self.assertEqual(status, 202)
                task = response["task_id"]
                # Each urllib response is closed: no browser connection remains attached to the worker.
                for _ in range(100):
                    job = request(prefix + "/jobs/" + task)[1]
                    if job["status"] not in {"queued", "running"}:
                        break
                    time.sleep(.02)
                self.assertEqual(job["status"], "completed", job)
                self.assertIn(b"##", request(prefix + "/jobs/" + task + "/report.md")[1])
                # Old HTML archives remain untouched; the URL renders their saved Markdown.
                archived = server.app.report_file(a, task, 'html')
                archived.write_text('<pre>legacy raw Markdown</pre>', encoding='utf-8')
                html_report = request(prefix + '/jobs/' + task + '/report.html')[1]
                self.assertIn(b'<article>', html_report)
                self.assertIn('报告目录'.encode(), html_report)
                self.assertNotIn(b'legacy raw Markdown', html_report)
                self.assertEqual(archived.read_text(encoding='utf-8'), '<pre>legacy raw Markdown</pre>')
                self.assertTrue(request(prefix + "/jobs/" + task + "/run")[1]["sources"])
                self.assertIn(b"run_finished", request(prefix + "/jobs/" + task + "/trace")[1])
                for path in ("/api/spaces/" + b + "/jobs/" + task, "/api/spaces/" + b + "/conversations/" + c + "/messages"):
                    with self.assertRaises(HTTPError) as denied:
                        request(path)
                    self.assertEqual(denied.exception.code, 404)
                with self.assertRaises(HTTPError) as denied:
                    request(messages, "POST", {"content": "你好"}, {"Origin": "https://evil.example"})
                self.assertEqual(denied.exception.code, 403)
                with self.assertRaises(HTTPError) as malformed:
                    request(messages, "POST", [])
                self.assertEqual(malformed.exception.code, 400)
                self.assertTrue(request("/api/runs")[1]["runs"])
                # A second process/port must not interrupt the active server's queue.
                queued = server.app.store.enqueue(a, c, "q", "brief", [])
                with self.assertRaises(OSError):
                    make_server(port=server.server_port, db_path=root / "app.db", trace_dir=root / "traces")
                with self.assertRaises(RuntimeError):
                    make_server(port=0, db_path=root / "app.db", trace_dir=root / "traces")
                self.assertEqual(server.app.store.job(a, queued["id"])["status"], "queued")
            finally:
                server.shutdown()
                server.app.close()
                server.server_close()
                thread.join(2)


if __name__ == "__main__":
    unittest.main()
