"""Real HTTP and ASGI lifecycle contracts for the FastAPI transport."""
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from fastapi.testclient import TestClient
from research_agent.workbench import OfflineRouter
from server import create_app, make_server


class FastAPIContracts(unittest.TestCase):
    def test_lifespan_is_lazy_exclusive_and_closes_workers(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"OFFLINE_MODE": "1"}):
            root = Path(tmp)
            options = dict(db_path=root/"app.db", trace_dir=root/"traces", model_factory=OfflineRouter)
            app = create_app(**options)
            self.assertFalse((root/"app.db").exists(), "Importing the ASGI app must not touch the database")
            with TestClient(app) as client:
                workbench = app.state.runtime.workbench
                self.assertTrue(workbench.worker.is_alive())
                self.assertTrue(workbench.coding.thread.is_alive())
                thread_id = workbench.worker.ident
                for _ in range(3):
                    self.assertEqual(client.get("/health").status_code, 200)
                    self.assertEqual(workbench.worker.ident, thread_id)
                space = workbench.store.save_space({"name": "Existing"})["id"]
                conversation = workbench.store.create_conversation(space, "Original")["id"]
                with self.assertRaisesRegex(RuntimeError, "已有工作台"):
                    with TestClient(create_app(**options)):
                        pass
                self.assertEqual(len(workbench.store.conversations(space)), 1)
            self.assertTrue(workbench.stop.is_set())
            self.assertFalse(workbench.worker.is_alive())
            self.assertFalse(workbench.coding.thread.is_alive())
            with TestClient(app) as client:
                self.assertIsNot(app.state.runtime.workbench, workbench)
                self.assertTrue(app.state.runtime.workbench.worker.is_alive())
                self.assertFalse(app.state.runtime.stopping.is_set())
                with self.assertRaisesRegex(RuntimeError, "已有工作台"):
                    with TestClient(create_app(**options)):
                        pass
                self.assertEqual(client.get(f"/api/spaces/{space}/conversations").json()["conversations"][0]["id"], conversation)

    def test_inflight_worker_keeps_database_lock_until_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            options = dict(db_path=root/"app.db", trace_dir=root/"traces", start_worker=False)
            runtime = create_app(**options).state.runtime
            runtime.open()
            original = runtime.workbench
            try:
                with patch.object(original.worker, "is_alive", return_value=True), patch.object(original.worker, "join"):
                    with self.assertRaisesRegex(RuntimeError, "保留数据库锁"):
                        runtime.close()
                    self.assertIs(runtime.workbench, original)
                    with self.assertRaisesRegex(RuntimeError, "仍在停止"):
                        runtime.open()
                    with self.assertRaisesRegex(RuntimeError, "已有工作台"):
                        create_app(**options).state.runtime.open()
            finally:
                runtime.close()
            self.assertIsNone(runtime.workbench)
            replacement = create_app(**options).state.runtime
            replacement.open()
            replacement.close()

    def test_openapi_typed_contracts_and_legacy_error_shape(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"OFFLINE_MODE": "1"}):
            root = Path(tmp)
            with TestClient(create_app(db_path=root/"state.db", trace_dir=root/"traces", start_worker=False)) as client:
                schema = client.get("/openapi.json").json()
                path = "/api/spaces/{space_id}/conversations/{conversation_id}/messages"
                self.assertIn(path, schema["paths"])
                self.assertEqual(schema["components"]["schemas"]["MessageBody"]["properties"]["background"]["type"], "boolean")
                self.assertIn("application/octet-stream", schema["paths"]["/api/spaces/{space_id}/materials/upload"]["post"]["requestBody"]["content"])
                for path, operations in schema["paths"].items():
                    for method, operation in operations.items():
                        if method in {"post", "patch", "delete"}:
                            self.assertIn("requestBody", operation, f"{method} {path}")
                            self.assertIn("400", operation["responses"])
                            self.assertNotIn("422", operation["responses"])
                docs = client.get("/docs")
                self.assertEqual(docs.status_code, 200)
                self.assertIn("SwaggerUIBundle", docs.text)
                self.assertIn("https://cdn.jsdelivr.net", docs.headers["content-security-policy"])
                self.assertNotIn("https://cdn.jsdelivr.net", client.get("/health").headers["content-security-policy"])
                space = client.post("/api/spaces", json={"name": "Test", "download_count": 7}).json()["id"]
                # Partial updates must not write schema defaults over saved settings.
                changed = client.patch(f"/api/spaces/{space}", json={"description": "Changed"}).json()
                self.assertEqual(changed["download_count"], 7)
                self.assertEqual(changed["name"], "Test")
                conv = client.post(f"/api/spaces/{space}/conversations", json={}).json()["id"]
                endpoint = f"/api/spaces/{space}/conversations/{conv}/messages"
                for body in ({"content": "hi", "background": "true"}, {"content": 12}, []):
                    response = client.post(endpoint, json=body)
                    self.assertEqual(response.status_code, 400, response.text)
                    self.assertIn("error", response.json())
                response = client.post("/api/spaces", json={"name": "x", "unknown": "sk-do-not-echo-this-private-value"})
                self.assertNotIn("sk-do-not-echo-this-private-value", response.text)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(client.get(endpoint).json()["messages"], [])
                self.assertEqual(client.get("/missing").status_code, 404)

    def test_body_and_origin_guards_precede_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with TestClient(create_app(db_path=root/"state.db", trace_dir=root/"traces", start_worker=False, model_factory=OfflineRouter)) as client:
                for headers, content, status in [
                    ({"Content-Type": "application/json", "Origin": "https://evil.example"}, '{"name":"x"}', 403),
                    ({"Content-Type": "application/json", "Sec-Fetch-Site": "cross-site"}, '{"name":"x"}', 403),
                    ({"Content-Type": "text/plain"}, '{"name":"x"}', 400),
                    ({"Content-Type": "application/json"}, '{"name":"' + "x"*100001 + '"}', 400),
                    ({"Content-Type": "application/json"}, "{broken", 400),
                    ({"Content-Type": "application/json", "Content-Length": "999"}, '{"name":"x"}', 400),
                ]:
                    response = client.post("/api/spaces", headers=headers, content=content)
                    self.assertEqual(response.status_code, status, response.text)
                self.assertEqual(client.get("/api/spaces").json()["spaces"], [])

    def test_sync_handler_does_not_block_health_and_server_closes(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"OFFLINE_MODE": "1"}):
            root = Path(tmp)
            server = make_server(host="localhost", port=0, db_path=root/"state.db", trace_dir=root/"traces", start_worker=False)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            entered, release = threading.Event(), threading.Event()
            address = f"http://127.0.0.1:{server.server_port}"
            errors = []
            def slow(_):
                entered.set()
                release.wait(5)
                return {"task_id": "fixture"}
            def request():
                try:
                    with urlopen(address+"/api/spaces", timeout=6) as response:
                        response.read()
                except Exception as exc:
                    errors.append(exc)
            try:
                with patch.object(server.app.store, "spaces", side_effect=slow):
                    caller = threading.Thread(target=request)
                    caller.start()
                    self.assertTrue(entered.wait(3))
                    with urlopen(address+"/health", timeout=2) as response:
                        self.assertEqual(response.status, 200)
                        self.assertEqual(json.load(response)["folder_picker"], os.name == "nt")
                    release.set()
                    caller.join(6)
                    self.assertEqual(errors, [])
            finally:
                release.set()
                server.shutdown()
                server.server_close()
                thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertTrue(server.finished.is_set())

    def test_public_bind_cannot_be_overridden_by_forwarded_headers(self):
        from research_agent.http_support import local_desktop
        from types import SimpleNamespace
        for host in (None, "0.0.0.0"):
            request = SimpleNamespace(headers={"host": "localhost:8000", "x-forwarded-for": "127.0.0.1"},
                client=SimpleNamespace(host="127.0.0.1"),
                app=SimpleNamespace(state=SimpleNamespace(runtime=SimpleNamespace(host=host))))
            self.assertFalse(local_desktop(request))


if __name__ == "__main__":
    unittest.main()
