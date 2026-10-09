"""HTTP contracts and transport helpers; domain rules remain in Workbench."""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from contextlib import closing
from pathlib import Path
from typing import Annotated
from urllib.parse import urlparse

from fastapi import Depends, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from .workbench_store import NotFound

Id = Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]


class RequestBody(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore")


class SpaceUpdate(RequestBody):
    model_config = ConfigDict(strict=True, extra="forbid")
    name: str = Field(default="", max_length=100)
    description: str = Field(default="", max_length=2000)
    download_root: str = Field(default="", max_length=2000)
    auto_download: bool | int = False
    download_count: int = Field(default=3, ge=1, le=20)
    download_mb: int = Field(default=50, ge=1, le=500)


class SpaceCreate(SpaceUpdate):
    name: str = Field(min_length=1, max_length=100)


class ConversationCreate(RequestBody):
    title: str = "新会话"


class ConversationUpdate(RequestBody):
    model_config = ConfigDict(strict=True, extra="forbid")
    title: str = Field(default="", max_length=100)
    archived: bool = False


class ForkBody(RequestBody):
    title: str | None = None
    through_message_id: int | None = None


class MessageBody(RequestBody):
    content: str = Field(min_length=1, max_length=4000)
    search_scope: str = "auto"
    model_id: str = "default"
    background: bool = False


class AutoResearchBody(RequestBody):
    model_config = ConfigDict(strict=True, extra="forbid")
    conversation_id: Id
    goal: str
    model_id: str = "default"
    budget: dict | None = None
    targets: list | None = None


class FolderBody(RequestBody):
    initial: str = ""


class ResearchBody(RequestBody):
    question: str = Field(min_length=1, max_length=4000)


def respond(status, data, content_type="application/json", attachment=None):
    headers = {"Content-Disposition": f'attachment; filename="{attachment}"'} if attachment else {}
    if isinstance(data, bytes):
        return Response(data, status_code=status, media_type=content_type, headers=headers)
    return JSONResponse(data, status_code=status, headers=headers)


def local_desktop(request: Request):
    hostname = urlparse("http://" + request.headers.get("host", "")).hostname
    runtime = request.app.state.runtime
    return (os.name == "nt" and hostname in {"localhost", "127.0.0.1", "::1"}
            and request.client is not None and request.client.host in {"127.0.0.1", "::1"}
            and runtime.host in {"127.0.0.1", "::1"})


def check_origin(headers):
    if headers.get("sec-fetch-site") == "cross-site":
        raise PermissionError("不接受跨站写入请求")
    origin = headers.get("origin")
    if origin and (urlparse(origin).netloc != headers.get("host") or urlparse(origin).scheme not in {"http", "https"}):
        raise PermissionError("请求来源不匹配")


def workbench(request: Request):
    return request.app.state.runtime.workbench


App = Annotated[object, Depends(workbench)]


def trace_path(request, raw):
    path = Path(raw)
    path = (path if path.is_absolute() else request.app.state.runtime.root / path).resolve()
    if not path.is_relative_to(workbench(request).trace_dir) or path.suffix != ".jsonl" or not path.is_file():
        raise NotFound("Trace 不存在或不在允许目录")
    return path


def trace_file(request, raw):
    return respond(200, trace_path(request, raw).read_bytes(), "application/x-ndjson", "trace.jsonl")


def trace_events(request, job, after):
    if not re.fullmatch(r"[0-9]{1,10}", after):
        raise ValueError("事件游标必须为非负整数")
    cursor, events = int(after), []
    if job["trace_path"]:
        with trace_path(request, job["trace_path"]).open(encoding="utf-8") as stream:
            for line in stream:
                if not line.endswith("\n"):
                    break
                record = json.loads(line)
                if record.get("seq", 0) > cursor:
                    events.append(record)
                    if len(events) == 200:
                        break
    return {"events": events, "next_seq": events[-1]["seq"] if events else cursor, "status": job["status"]}


def conversation_stream(request, space_id, conversation_id):
    app = workbench(request)
    app.store.history(space_id, conversation_id, 1)
    check_origin(request.headers)
    cursor = request.headers.get("last-event-id") or request.query_params.get("after", "0")
    if not re.fullmatch(r"[0-9]{1,18}", cursor):
        raise ValueError("事件游标必须为非负整数")
    after = int(cursor)
    with closing(app.store._connect()) as db:
        if not after:
            first = db.execute("SELECT min(e.id) FROM conversation_events e JOIN research_jobs j ON j.id=e.job_id WHERE e.conversation_id=? AND j.status IN ('running','queued')", (conversation_id,)).fetchone()[0]
            after = first - 1 if first else db.execute("SELECT coalesce(max(id),0) FROM conversation_events WHERE conversation_id=?", (conversation_id,)).fetchone()[0]
    kinds = {"model_stream", "answer_draft", "model_request", "tool_call_requested", "tool_result",
             "answer_check_started", "answer_checked", "answer_repair", "answer_check_unavailable",
             "context_compacted", "material_progress", "material_failed", "model_error", "run_finished", "message"}

    def batch(cursor):
        with closing(app.store._connect()) as db:
            jobs = [dict(r) for r in db.execute("SELECT id,status,stage,recent_action,model_name FROM research_jobs WHERE conversation_id=? AND status IN ('running','queued') ORDER BY created_at,turn_seq,id", (conversation_id,))]
            rows = [dict(r) for r in db.execute("SELECT id,job_id,kind,payload,created_at FROM conversation_events WHERE conversation_id=? AND id>? ORDER BY id LIMIT 200", (conversation_id, cursor))]
        return jobs, rows

    def emit(event, payload, seq=None):
        return ((f"id: {seq}\n" if seq is not None else "") + "event: " + event +
                "\ndata: " + json.dumps(payload, ensure_ascii=False) + "\n\n").encode("utf-8")

    async def events():
        nonlocal after
        previous, until, heartbeat = None, time.monotonic() + 45, time.monotonic()
        while not app.stop.is_set() and not request.app.state.runtime.stopping.is_set() and time.monotonic() < until:
            jobs, rows = await run_in_threadpool(batch, after)
            if jobs != previous:
                yield emit("jobs", {"jobs": jobs})
                previous = jobs
            for row in rows:
                after = row["id"]
                if row["kind"] in kinds:
                    payload = json.loads(row["payload"])
                    if row["kind"] == "tool_result":
                        payload = {k: payload[k] for k in ("name", "ok", "title", "error") if k in payload}
                    elif row["kind"] == "answer_checked":
                        payload = {"passed": payload.get("passed"), "state": payload.get("state")}
                    yield emit("progress", {"job_id": row["job_id"], "kind": row["kind"], "payload": payload, "ts": row["created_at"]}, after)
            if rows or time.monotonic() - heartbeat >= 10:
                yield emit("cursor", {}, after)
                heartbeat = time.monotonic()
            await asyncio.sleep(.15 if len(rows) < 200 else .01)

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"})
