"""Per-message provider selection, durable jobs, and credential isolation."""
import json
import os
import tempfile
import threading
import unittest
from contextlib import closing
from io import BytesIO
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from research_agent import FixtureReader, FixtureSearch, ResearchAgent
from research_agent.models import model_from_env, model_options
from research_agent.workbench_store import WorkbenchStore
from server import make_server

ROOT = Path(__file__).resolve().parents[1]
ENV = {"OFFLINE_MODE": "0", "PROVIDER": "deepseek", "MODEL": "deepseek-flash",
       "BASE_URL": "https://api.deepseek.com/v1", "API_KEY": "test-deepseek-private-key",
       "SUDOCODE_API_KEY": "test-sudocode-private-key", "SUDOCODE_BASE_URL": "https://api.sudocode.chat/v1",
       **{"SUDOCODE_"+v.upper()+"_MODEL": "gpt-5.6-"+v for v in ("luna", "terra", "sol")}}


class ModelSelectionTests(unittest.TestCase):
    def setUp(self):
        for name in ("research_agent.models.load_dotenv", "server.load_dotenv"):
            replacement = patch(name)
            replacement.start()
            self.addCleanup(replacement.stop)

    def test_credentials_are_scoped_and_catalog_is_public(self):
        with patch.dict(os.environ, ENV):
            default = model_from_env()
            self.assertEqual(default.api_key, ENV["API_KEY"])
            self.assertEqual(default.model, "deepseek-flash")
            for variant in ("luna", "terra", "sol"):
                client = model_from_env("sudocode-" + variant)
                self.assertEqual(client.api_key, ENV["SUDOCODE_API_KEY"])
                self.assertEqual(client.base_url, "https://api.sudocode.chat/v1/")
                self.assertEqual(client.model, "gpt-5.6-" + variant)
            data = model_options()
            self.assertEqual(len(data["models"]), 4)
            self.assertTrue(all(m["configured"] for m in data["models"]))
            self.assertNotIn(ENV["API_KEY"], json.dumps(data))
            self.assertNotIn(ENV["SUDOCODE_API_KEY"], json.dumps(data))
            with patch.dict(os.environ, {"SUDOCODE_LUNA_MODEL": "proxy-custom-luna"}):
                self.assertEqual(model_from_env("sudocode-luna").model, "proxy-custom-luna")
            self.assertEqual(os.environ["MODEL"], "deepseek-flash")

    def test_gateway_rejection_is_distinguished_from_model_refusal(self):
        blocked = HTTPError('https://api.sudocode.chat/v1/chat/completions', 403, 'Forbidden', {}, BytesIO(b'error code: 1010'))
        with patch.dict(os.environ, ENV), patch('research_agent.models.urlopen', side_effect=blocked):
            with self.assertRaisesRegex(RuntimeError, '网关.*403 / 1010'):
                model_from_env('sudocode-luna').complete([{'role':'user','content':'hello'}], [])

    def test_sudocode_timeout_is_configurable_and_reaches_transport(self):
        with patch.dict(os.environ, {**ENV, 'SUDOCODE_TIMEOUT_SECONDS': ''}):
            self.assertEqual(model_from_env('sudocode-luna').timeout, 180)
            self.assertEqual(model_from_env().timeout, 45)
            with patch.dict(os.environ, {'SUDOCODE_TIMEOUT_SECONDS': '240'}), patch(
                'research_agent.models.urlopen', side_effect=TimeoutError('The read operation timed out')
            ) as transport:
                with self.assertRaisesRegex(RuntimeError, '响应超时.*240 秒'):
                    model_from_env('sudocode-sol').complete([{'role':'user','content':'hello'}], [])
                self.assertEqual(transport.call_args.kwargs['timeout'], 240)
            for value in ('0', '-1', 'nan', 'inf', 'bad'):
                with self.subTest(value=value), patch.dict(os.environ, {'SUDOCODE_TIMEOUT_SECONDS': value}):
                    with self.assertRaisesRegex(ValueError, 'SUDOCODE_TIMEOUT_SECONDS'):
                        model_from_env('sudocode-terra')

    def test_missing_key_and_unknown_model_never_fall_back(self):
        with patch.dict(os.environ, {**ENV, "SUDOCODE_API_KEY": ""}):
            for variant in ("luna", "terra", "sol"):
                with self.assertRaisesRegex(ValueError, "SUDOCODE_API_KEY"):
                    model_from_env("sudocode-" + variant)
            self.assertEqual([m["configured"] for m in model_options()["models"]], [True, False, False, False])
            for invalid in (None, [], "gpt-unknown", "https://evil.example"):
                with self.assertRaises(ValueError):
                    model_from_env(invalid)
        with patch.dict(os.environ, {**ENV, "OFFLINE_MODE": "1"}):
            with self.assertRaisesRegex(ValueError, "离线演示"):
                model_from_env("sudocode-luna")

    def test_legacy_records_migrate_without_guessing_their_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = WorkbenchStore(Path(tmp)/"app.db")
            space = store.save_space({"name": "Legacy"})["id"]
            conversation = store.create_conversation(space, "Legacy")["id"]
            job = store.enqueue(space, conversation, "question", "brief", [])
            with closing(store._connect()) as db, db:
                for table in ("messages", "research_jobs"):
                    db.execute("ALTER TABLE " + table + " DROP COLUMN model_id")
                    db.execute("ALTER TABLE " + table + " DROP COLUMN model_name")
            migrated = WorkbenchStore(store.path)
            self.assertEqual(migrated.job(space, job["id"])["model_id"], "default")
            self.assertEqual(migrated.job(space, job["id"])["model_name"], "")
            self.assertEqual(migrated.history(space, conversation)[0]["content"], store.history(space, conversation)[0]["content"])

    def test_http_selection_survives_queue_switches_and_retry(self):
        captured = []
        def provider(request, **kwargs):
            # This gateway rejects the default Python-urllib identity before parsing the model request.
            self.assertEqual(request.get_header("User-agent"), "ResearchAgent/1.0")
            body = json.loads(request.data)
            self.assertEqual(request.get_header("Accept"), "text/event-stream" if body.get('stream') else "application/json")
            captured.append((request.full_url, request.get_header("Authorization"), body))
            if "意图路由器" in body["messages"][0]["content"]:
                chat = body["messages"][-1]["content"] == "你好"
                route = {"intent": "CHAT" if chat else "RESEARCH", "reply": "你好" if chat else "",
                         "brief": "核实 Agent 教程" if not chat else "", "assumptions": [], "effort": "none" if chat else "quick"}
                message = {"content": json.dumps(route)}
            elif "你是回答核验员" in body["messages"][0]["content"]:
                data=json.loads(body['messages'][-1]['content'])['UNTRUSTED_DOCUMENT_DATA']
                ev=next(e for e in data['evidence'] if e['kind']=='page')
                block=data['blocks'][0]['block_id']
                message={'content':json.dumps({'requirements':[{'id':'R1','requirement':'tutorial','addressed':True,'block_ids':[block],'reason':'covered'}],
                    'blocks':[{'block_id':block,'kind':'fact','evidence_ids':[ev['evidence_id']],'supported':True,'reason':'Read tutorial body.'}]})}
            elif not any(m["role"] == "tool" for m in body["messages"]):
                message = {"tool_calls": [{"id": "s1", "type": "function", "function": {"name": "search", "arguments": '{"query":"Agent"}'}}]}
            else:
                payload=json.loads(next(m['content'] for m in reversed(body['messages']) if m['role']=='tool'))['UNTRUSTED_TOOL_DATA']
                if payload.get('results'):
                    message={'tool_calls':[{'id':'r1','type':'function','function':{'name':'read','arguments':json.dumps({'url':payload['results'][0]['url']})}}]}
                else:
                    message = {"content": "This tutorial builds an agent incrementally. [S1] ["+payload['evidence_id']+"]"}
            return BytesIO(json.dumps({"choices": [{"message": message}]}).encode())

        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, ENV), patch("research_agent.models.urlopen", side_effect=provider):
            root = Path(tmp)
            def agent(observer, model_id="default"):
                return ResearchAgent(FixtureSearch.from_file(ROOT/"fixtures/search_results.json"), model_from_env(model_id), root/"traces",
                                     reader=FixtureReader.from_file(ROOT/"fixtures/search_results.json"), on_event=observer)
            server = make_server(port=0, db_path=root/"app.db", trace_dir=root/"traces", agent_factory=agent, start_worker=False)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            address = "http://127.0.0.1:" + str(server.server_port)
            def request(path, data=None):
                req = Request(address+path, data=json.dumps(data).encode() if data is not None else None,
                              headers={"Content-Type": "application/json"})
                with urlopen(req, timeout=5) as response:
                    return json.load(response)
            try:
                self.assertEqual(len(request("/api/models")["models"]), 4)
                space = request("/api/spaces", {"name": "Switch test"})["id"]
                conversation = request(f"/api/spaces/{space}/conversations", {})["id"]
                prefix = f"/api/spaces/{space}"
                messages = prefix+f"/conversations/{conversation}/messages"
                for invalid in (None, [], "unknown"):
                    with self.assertRaises(HTTPError) as failure:
                        request(messages, {"content": "test", "model_id": invalid})
                    self.assertEqual(failure.exception.code, 400)
                with patch.dict(os.environ, {"SUDOCODE_API_KEY": ""}):
                    with self.assertRaises(HTTPError):
                        request(messages, {"content": "test", "model_id": "sudocode-luna"})
                self.assertEqual(request(messages)["messages"], [])
                luna = request(messages, {"content": "研究 Agent", "model_id": "sudocode-luna"})["job"]
                terra = request(messages, {"content": "继续研究", "model_id": "sudocode-terra"})["job"]
                sol = request(messages, {"content": "你好", "model_id": "sudocode-sol"})
                self.assertEqual(sol["job"]["model_name"], "gpt-5.6-sol")
                self.assertEqual(sol["intent"], "QUEUED")
                request(prefix+"/jobs/"+luna["id"]+"/cancel", {})
                retry = request(prefix+"/jobs/"+luna["id"]+"/retry", {})
                self.assertEqual(retry["model_id"], "sudocode-luna")
                self.assertEqual(retry["model_name"], luna["model_name"])
                for expected in (terra, sol["job"], retry):
                    server.app.execute(server.app.store.claim_next())
                    job = request(prefix+"/jobs/"+expected["id"])
                    self.assertEqual(job["status"], "completed", job["error"])
                    history = request(messages)["messages"]
                    self.assertEqual([m for m in history if m["job_id"]==expected["id"]][-1]["model_name"], expected["model_name"])
                request(messages, {"content": "你好"})  # Old clients retain the existing provider.
                self.assertEqual(captured[-1][2]["model"], "deepseek-flash")
                for url, authorization, body in captured:
                    if body["model"].startswith("gpt-"):
                        self.assertEqual(url, "https://api.sudocode.chat/v1/chat/completions")
                        self.assertEqual(authorization, "Bearer "+ENV["SUDOCODE_API_KEY"])
                        self.assertNotIn("temperature", body)
                    else:
                        self.assertEqual(authorization, "Bearer "+ENV["API_KEY"])
                self.assertEqual({body["model"] for _, _, body in captured if body.get("tools")}, {"gpt-5.6-luna", "gpt-5.6-terra"})
            finally:
                server.shutdown(); server.server_close(); thread.join(2)


if __name__ == "__main__":
    unittest.main()
