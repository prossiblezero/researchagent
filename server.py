"""FastAPI entrypoint. One process owns the existing SQLite-backed Workbench."""
from __future__ import annotations

import asyncio
import json
import os
import re
import socket
import threading
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.openapi.utils import get_openapi
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.datastructures import Headers, MutableHeaders

from main import build_search
from research_agent import FixtureReader, HttpReader, OfflineModel, ResearchAgent, load_dotenv, model_from_env
from research_agent.trace import redact
from research_agent.materials import MAX_FILE_BYTES
from research_agent.workbench import OfflineRouter, Workbench
from research_agent.workbench_store import Conflict, NotFound, WorkbenchStore
from research_agent.http_support import check_origin, respond

ROOT = Path(__file__).resolve().parent


def offline_mode():
    return os.getenv("OFFLINE_MODE", "").strip().lower() in {"1", "true", "yes", "on"}


def live_model(model_id="default"):
    model = model_from_env(model_id)
    if isinstance(model, OfflineModel) and not offline_mode():
        raise ValueError("请配置模型凭据，或显式设置 OFFLINE_MODE=1 使用演示")
    return model


class Runtime:
    def __init__(self, host, db_path, trace_dir, model_factory, agent_factory, start_worker):
        self.root, self.host = ROOT, host
        self.db_path, self.trace_dir = db_path, trace_dir
        self.model_factory, self.agent_factory = model_factory, agent_factory
        self.start_worker = start_worker
        self.workbench = self.database_lock = None
        self.stopping = threading.Event()

    def open(self):
        if self.workbench is not None:
            if self.stopping.is_set():
                raise RuntimeError("上一轮工作台仍在停止，请等待任务线程退出")
            return
        load_dotenv(self.root / ".env")
        path = Path(self.db_path or os.getenv("DB_PATH", str(self.root / "data/evidence_agent.db"))).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        # Acquire ownership before schema initialization or recovery can alter a live queue.
        lock = open(str(path) + ".lock", "a+b")
        try:
            if lock.tell() == 0:
                lock.write(b"0")
                lock.flush()
            lock.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            lock.close()
            raise RuntimeError("此数据库已有工作台服务运行，请使用原服务或独立数据库") from exc
        self.database_lock = lock
        self.stopping.clear()
        try:
            def default_model(model_id="default"):
                return OfflineRouter() if offline_mode() and model_id == "default" else live_model(model_id)

            def default_agent(observer, model_id="default"):
                model, offline = live_model(model_id), offline_mode()
                if not offline and not any(os.getenv(k) for k in ("TAVILY_API_KEY", "SEARCH_API_KEY", "SEARCH_URL")):
                    raise ValueError("请配置真实搜索服务，或显式启用离线演示")
                reader = FixtureReader.from_file(self.root / "fixtures/search_results.json") if offline else HttpReader()
                return ResearchAgent(build_search(), model, trace_dir=trace_dir, reader=reader, max_tool_calls=16, max_rounds=20, on_event=observer)

            trace_dir = Path(self.trace_dir or self.root / "traces").resolve()
            self.workbench = Workbench(WorkbenchStore(path), self.model_factory or default_model,
                                       self.agent_factory or default_agent, trace_dir, start_worker=False)
            if self.start_worker:
                self.workbench.coding.start()
                self.workbench.worker.start()
        except BaseException:
            self.close()
            raise

    def close(self):
        self.stopping.set()
        if self.workbench is not None:
            self.workbench.close()
            # close() may have been preceded by an explicit stop, or timed out on a model call.
            if not self.workbench.coding.stop.is_set():
                self.workbench.coding.close()
            workers = (self.workbench.worker, self.workbench.parallel_worker, self.workbench.coding.thread)
            for worker in workers:
                if worker is not None and worker.is_alive():
                    worker.join(timeout=2)
            if any(worker is not None and worker.is_alive() for worker in workers):
                raise RuntimeError("后台线程尚未停止，保留数据库锁以避免重复执行")
            self.workbench = None
        if self.database_lock is not None:
            self.database_lock.close()
            self.database_lock = None


def error_response(exc):
    if isinstance(exc, RequestValidationError):
        # Do not echo request inputs (which may contain credentials) in validation errors.
        detail = "; ".join(".".join(map(str, e["loc"])) + ": " + e["msg"] for e in exc.errors())
        return respond(400, {"error": str(redact(detail))[:600]})
    status = (exc.status_code if isinstance(exc, HTTPException) else
              404 if isinstance(exc, (NotFound, FileNotFoundError)) else
              409 if isinstance(exc, Conflict) else 403 if isinstance(exc, PermissionError) else
              400 if isinstance(exc, (ValueError, UnicodeError)) else 500)
    detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
    return respond(status, {"error": str(redact(str(detail)))[:600]})


CSP = "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"


class HTTPPolicy:
    """Bound request bodies before parsing; retain same-origin writes and security headers."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def secured(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["X-Content-Type-Options"] = "nosniff"
                headers.setdefault("Cache-Control", "no-store")
                policy = CSP
                if scope["path"] == "/docs":
                    policy = policy.replace("style-src 'self' 'unsafe-inline'", "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net").replace("script-src 'self' 'unsafe-inline'", "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net")
                headers["Content-Security-Policy"] = policy
            await send(message)

        if scope["method"] in {"POST", "PATCH", "DELETE"}:
            try:
                headers = Headers(scope=scope)
                upload = scope["method"] == "POST" and re.fullmatch(r"/api/spaces/[a-f0-9]{32}/materials/upload", scope["path"])
                expected = "application/octet-stream" if upload else "application/json"
                size = int(headers.get("content-length", "0"))
                limit = MAX_FILE_BYTES if upload else 100000
                if not 0 < size <= limit:
                    raise ValueError("导入文件应在 25 MB 以内" if upload else "请求体大小不合法")
                chunks, seen = [], 0
                async with asyncio.timeout(60):
                    while True:
                        message = await receive()
                        if message["type"] == "http.disconnect":
                            return
                        chunk = message.get("body", b"")
                        seen += len(chunk)
                        if seen > size:
                            raise ValueError("请求体大小不合法")
                        chunks.append(chunk)
                        if not message.get("more_body", False):
                            break
                if seen != size:
                    raise ValueError("请求体不完整，未保存")
                # Finish bounded input before rejecting it; otherwise Windows can reset
                # a Connection: close socket before the client receives the JSON error.
                check_origin(headers)
                if headers.get("content-type", "").split(";", 1)[0].strip().lower() != expected:
                    raise ValueError("请求必须使用 " + expected)
                if upload and not headers.get("origin"):
                    raise ValueError("请通过资料库的文件选择按钮导入资料")
                raw = b"".join(chunks)
                if not upload and not isinstance(json.loads(raw), dict):
                    raise ValueError("请求体必须为 JSON 对象")
            except (ValueError, UnicodeError, PermissionError, TimeoutError) as exc:
                return await error_response(exc)(scope, receive, secured)

            original_receive, delivered = receive, False

            async def replay():
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {"type": "http.request", "body": raw, "more_body": False}
                return await original_receive()

            receive = replay
        await self.app(scope, receive, secured)


def create_app(*, host=None, db_path=None, trace_dir=None, model_factory=None, agent_factory=None, start_worker=True):
    # A directly imported ASGI app is conservative: local execution requires an explicit bind host.
    runtime = Runtime(host, db_path, trace_dir, model_factory, agent_factory, start_worker)

    @asynccontextmanager
    async def lifespan(app):
        await run_in_threadpool(runtime.open)
        try:
            yield
        finally:
            await run_in_threadpool(runtime.close)

    api = FastAPI(title="ResearchAgent API", version="v4", lifespan=lifespan,
                  docs_url=None, redoc_url=None, redirect_slashes=False)
    api.state.runtime = runtime
    api.add_middleware(HTTPPolicy)

    async def handle_error(request, exc):
        return error_response(exc)

    for kind in (RequestValidationError, HTTPException, NotFound, FileNotFoundError, Conflict,
                 PermissionError, ValueError, UnicodeError, Exception):
        api.add_exception_handler(kind, handle_error)

    @api.get("/docs", include_in_schema=False)
    def docs():
        return get_swagger_ui_html(openapi_url="/openapi.json", title="ResearchAgent API",
                                   swagger_favicon_url="/assistant.svg")

    from research_agent.http_api import router
    from research_agent.http_records import router as records_router
    api.include_router(router)
    api.include_router(records_router)

    def openapi():
        if api.openapi_schema is None:
            schema = get_openapi(title=api.title, version=api.version, routes=api.routes)
            for operations in schema["paths"].values():
                for operation in operations.values():
                    responses = operation.get("responses", {})
                    if "422" in responses:
                        responses.pop("422")
                        responses["400"] = {"description": "Invalid request", "content": {"application/json": {
                            "schema": {"type": "object", "required": ["error"], "properties": {"error": {"type": "string"}}}}}}
            api.openapi_schema = schema
        return api.openapi_schema

    api.openapi = openapi
    return api


class WorkbenchServer:
    """Keep the existing embedded-server/test API, backed entirely by Uvicorn."""
    def __init__(self, host, port, **kwargs):
        self.serving, self.finished = threading.Event(), threading.Event()
        self.serving_thread = None
        self.socket = socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET, socket.SOCK_STREAM)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE if os.name == "nt" else socket.SO_REUSEADDR, 1)
        try:
            self.socket.bind((host, port))
            self.socket.listen(128)
            self.server_address = self.socket.getsockname()
            self.server_port = self.server_address[1]
            self.asgi_app = create_app(host=self.server_address[0], **kwargs)
            self.runtime = self.asgi_app.state.runtime
            self.runtime.open()
            self.app = self.runtime.workbench
            self.engine = uvicorn.Server(uvicorn.Config(self.asgi_app, host=host, port=self.server_port,
                # Windows Proactor can leak transports after a peer reset.
                # This local HTTP loop needs sockets only; coding subprocesses run in workers.
                loop="asyncio:SelectorEventLoop" if os.name == "nt" else "auto",
                proxy_headers=False, access_log=False, log_level="warning", ws="none",
                timeout_graceful_shutdown=5))
        except BaseException:
            self.socket.close()
            if hasattr(self, "runtime"):
                self.runtime.close()
            raise

    def serve_forever(self):
        self.serving_thread = threading.current_thread()
        self.serving.set()
        try:
            self.engine.run(sockets=[self.socket])
        finally:
            try:
                self.runtime.close()
            finally:
                self.finished.set()

    def shutdown(self):
        self.runtime.stopping.set()
        self.engine.should_exit = True
        if self.serving.is_set() and threading.current_thread() is not self.serving_thread:
            if not self.finished.wait(30):
                raise RuntimeError("HTTP 服务尚未停止，保留数据库锁以避免重复执行")

    def server_close(self):
        self.shutdown()
        self.runtime.close()
        self.socket.close()


def make_server(host="127.0.0.1", port=8000, **kwargs):
    return WorkbenchServer(host, port, **kwargs)


if __name__ == "__main__":
    load_dotenv(ROOT / ".env")
    host, port = os.getenv("HOST", "127.0.0.1"), int(os.getenv("PORT", "8000"))
    server = make_server(host, port)
    print(f"ResearchAgent V4 (FastAPI): http://{host}:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
