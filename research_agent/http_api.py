"""Workbench HTTP routes; synchronous business calls run in FastAPI's thread pool."""
from __future__ import annotations

import html
import os
from contextlib import closing
from typing import Annotated

from fastapi import APIRouter, Body, Request
from starlette.concurrency import run_in_threadpool

from . import http_support as http
from .http_support import (App, Id, SpaceCreate, SpaceUpdate, ConversationCreate, ConversationUpdate,
                           MessageBody, ForkBody, AutoResearchBody, FolderBody, ResearchBody, respond)
from .folders import choose_folder
from .models import model_options, validate_model_id
from .reports import render_report
from .trace import now_iso
from .workbench_store import Conflict, NotFound, text_field

router = APIRouter()
JsonBody = Annotated[dict, Body()]
SPACE = "/api/spaces/{space_id}"
CONV = SPACE + "/conversations/{conversation_id}"
JOB = SPACE + "/jobs/{job_id}"


@router.get("/", include_in_schema=False)
def index(request: Request):
    return respond(200, (request.app.state.runtime.root / "web/index.html").read_bytes(), "text/html")


@router.get("/style.css", include_in_schema=False)
@router.get("/app.js", include_in_schema=False)
@router.get("/research-records.js", include_in_schema=False)
@router.get("/strategies.js", include_in_schema=False)
@router.get("/highlight.min.js", include_in_schema=False)
@router.get("/assistant.svg", include_in_schema=False)
def asset(request: Request):
    name = request.url.path[1:]
    media = "image/svg+xml" if name.endswith(".svg") else "text/css" if name.endswith(".css") else "text/javascript"
    return respond(200, (request.app.state.runtime.root / "web" / name).read_bytes(), media)


@router.get("/evaluation/{filename:path}", include_in_schema=False)
@router.get("/v3-evaluation/{filename:path}", include_in_schema=False)
@router.get("/harness-evaluation/{filename:path}", include_in_schema=False)
@router.get("/v4-evaluation/{filename:path}", include_in_schema=False)
@router.get("/evidence-qa-evaluation/{filename:path}", include_in_schema=False)
def evaluation(request: Request, filename: str):
    directories = {"evaluation": "context-memory-optimization-20260918", "v3-evaluation": "roadmap-v3-20260921",
                   "harness-evaluation": "harness-strategies-20260921", "v4-evaluation": "v4-first-cycle-20260923",
                   "evidence-qa-evaluation": "evidence-qa-20260927"}
    directory = (request.app.state.runtime.root / "evals/reports" / directories[request.url.path.split("/")[1]]).resolve()
    file = (directory / filename).resolve()
    media = {".html": "text/html", ".md": "text/markdown", ".json": "application/json",
             ".jsonl": "application/x-ndjson", ".log": "text/plain", ".patch": "text/plain", ".py": "text/plain"}
    if not file.is_relative_to(directory) or file.suffix not in media or not file.is_file():
        raise NotFound("评测文件不存在")
    return respond(200, file.read_bytes(), media[file.suffix])


@router.get("/health", tags=["Service"])
def health(request: Request):
    offline = os.getenv("OFFLINE_MODE", "").strip().lower() in {"1", "true", "yes", "on"}
    return {"status": "ok", "version": "v4", "mode": "offline-demo" if offline else "live",
            "folder_picker": http.local_desktop(request)}


@router.get("/api/models", tags=["Service"])
def models():
    return model_options()


@router.post("/api/folders/pick", tags=["Service"])
def pick_folder(request: Request, body: FolderBody):
    if not http.local_desktop(request) or not request.headers.get("origin"):
        raise PermissionError("目录选择仅支持从本机 Windows 的工作台页面打开")
    return choose_folder(body.initial)


@router.get("/api/spaces", tags=["Spaces"])
def spaces(app: App, state: str = "active"):
    return {"spaces": app.store.spaces(state)}


@router.post("/api/spaces", status_code=201, tags=["Spaces"])
def create_space(app: App, body: SpaceCreate):
    return app.store.save_space(body.model_dump(exclude_unset=True))


@router.get(SPACE, tags=["Spaces"])
def space(app: App, space_id: Id):
    return app.store.space(space_id)


@router.patch(SPACE, tags=["Spaces"])
def update_space(app: App, space_id: Id, body: SpaceUpdate):
    return app.store.save_space(body.model_dump(exclude_unset=True), space_id)


@router.delete(SPACE, tags=["Spaces"])
def delete_space(app: App, space_id: Id, body: JsonBody):
    if not app.routing.acquire(blocking=False):
        raise Conflict("正在处理消息，请稍后删除研究区")
    try:
        app.store.delete_space(space_id)
    finally:
        app.routing.release()
    return {"deleted": True}


@router.post(SPACE + "/archive", tags=["Spaces"])
@router.post(SPACE + "/restore", tags=["Spaces"])
def space_state(request: Request, app: App, space_id: Id, body: JsonBody):
    return app.store.set_space_state(space_id, "archived" if request.url.path.endswith("/archive") else "active")


@router.get(SPACE + "/recycle", tags=["Spaces"])
def recycle(app: App, space_id: Id):
    app.store.space(space_id)
    with closing(app.store._connect()) as db:
        turns = [dict(r) for r in db.execute("""SELECT m.conversation_id,m.turn_seq,c.title,min(m.id) id,
            max(CASE WHEN m.role='user' THEN m.content ELSE '' END) content
            FROM messages m JOIN conversations c ON c.id=m.conversation_id
            WHERE c.space_id=? AND c.deleted_at IS NULL AND m.deleted_at IS NOT NULL
            GROUP BY m.conversation_id,m.turn_seq ORDER BY id DESC""", (space_id,))]
    conversations = app.store.conversations(space_id, True)
    return {"branches": [b for b in conversations if b["deleted_at"]],
            "archived": [b for b in conversations if b["archived_at"] and not b["deleted_at"]], "turns": turns}


@router.get(SPACE + "/conversations", tags=["Sessions"])
def conversations(app: App, space_id: Id):
    return {"conversations": app.store.conversations(space_id)}


@router.post(SPACE + "/conversations", status_code=201, tags=["Sessions"])
def create_conversation(app: App, space_id: Id, body: ConversationCreate):
    return app.store.create_conversation(space_id, body.title)


@router.patch(CONV, tags=["Sessions"])
def update_conversation(app: App, space_id: Id, conversation_id: Id, body: ConversationUpdate):
    return app.sessions.update(space_id, conversation_id, body.model_dump(exclude_unset=True))


@router.delete(CONV, tags=["Sessions"])
def delete_conversation(app: App, space_id: Id, conversation_id: Id, body: JsonBody):
    return app.sessions.delete_branch(space_id, conversation_id)


@router.post(CONV + "/restore", tags=["Sessions"])
def restore_conversation(app: App, space_id: Id, conversation_id: Id, body: JsonBody):
    return app.sessions.restore_branch(space_id, conversation_id)


@router.post(CONV + "/fork", status_code=201, tags=["Sessions"])
def fork(app: App, space_id: Id, conversation_id: Id, body: ForkBody):
    return app.sessions.fork(space_id, conversation_id, body.title, body.through_message_id)


@router.get(CONV + "/context", tags=["Sessions"])
def context(app: App, space_id: Id, conversation_id: Id):
    return app.sessions.context(space_id, conversation_id)


@router.get(CONV + "/events", tags=["Sessions"], response_class=http.StreamingResponse)
def events(request: Request, space_id: Id, conversation_id: Id):
    return http.conversation_stream(request, space_id, conversation_id)


@router.get(CONV + "/messages", tags=["Sessions"])
def messages(app: App, space_id: Id, conversation_id: Id):
    return {"messages": app.store.history(space_id, conversation_id)}


@router.post(CONV + "/messages", tags=["Sessions"], responses={202: {"description": "Queued task"}})
def send(request: Request, app: App, space_id: Id, conversation_id: Id, body: MessageBody):
    result = app.send(space_id, conversation_id, body.content, body.search_scope, body.model_id,
                      background=body.background, allow_execution=http.local_desktop(request))
    return respond(202 if result.get("task_id") else 200, result)


@router.delete(CONV + "/messages/{message_id}", tags=["Sessions"])
def delete_turn(app: App, space_id: Id, conversation_id: Id, message_id: int, body: JsonBody):
    return app.sessions.delete_turn(space_id, conversation_id, message_id)


@router.post(CONV + "/turns/{turn_seq}/restore", tags=["Sessions"])
def restore_turn(app: App, space_id: Id, conversation_id: Id, turn_seq: int, body: JsonBody):
    return app.sessions.restore_turn(space_id, conversation_id, turn_seq)


@router.post(CONV + "/stop", tags=["Sessions"])
def stop(app: App, space_id: Id, conversation_id: Id, body: JsonBody):
    app.store.history(space_id, conversation_id, 1)
    stopped = []
    for item in app.store.jobs(space_id):
        if item["conversation_id"] == conversation_id and item["status"] in {"queued", "running"}:
            try:
                stopped.append(app.store.cancel(space_id, item["id"])["id"])
            except Conflict:
                pass  # Completion can win the race with cancellation.
    return {"stopped": stopped}


@router.get(SPACE + "/retrieval", tags=["Memory"])
def retrieval(app: App, space_id: Id):
    app.store.space(space_id)
    return app.retrieval.status(space_id)


@router.get(SPACE + "/memories", tags=["Memory"])
def memories(app: App, space_id: Id):
    return {"memories": app.memory.list(space_id, include_global=True)}


@router.patch(SPACE + "/memories/{memory_id}", tags=["Memory"])
def update_memory(app: App, space_id: Id, memory_id: Id, body: JsonBody):
    return app.memory.update(space_id, memory_id, body)


@router.delete(SPACE + "/memories/{memory_id}", tags=["Memory"])
def forget_memory(app: App, space_id: Id, memory_id: Id, body: JsonBody):
    return app.memory.forget(space_id, memory_id)


@router.get(SPACE + "/history/{item_id}", tags=["Memory"])
@router.get(SPACE + "/memory/{item_id}", tags=["Memory"])
def recalled(request: Request, app: App, space_id: Id, item_id: Id):
    origin = app.store.space(space_id)
    if request.url.path.split("/")[-2] == "history":
        history = app.store.history(space_id, item_id)
        content = "\n\n".join("## " + ("用户提问" if m["role"] == "user" else "助手回答") + "\n\n" + m["content"] for m in history if m["intent"] != "PENDING")
    else:
        item = next((m for m in app.memory.list(space_id) if m["id"] == item_id), None)
        if item is None:
            raise NotFound("记忆不存在或已删除")
        content = item["content"] + "\n\n## 来源\n\n" + "\n\n".join(str(r.get("quote", "")) for r in item["source_refs"])
    return respond(200, render_report("来源研究区：" + origin["name"] + "\n\n历史内容仅供回溯，不代表当前事实核验。\n\n" + content,
                                     "研究历史 · " + origin["name"]).encode("utf-8"), "text/html")


@router.get(SPACE + "/materials", tags=["Materials"])
def materials(app: App, space_id: Id):
    app.library.sync_reports(space_id)
    return {"materials": app.library.list(space_id)}


@router.post(SPACE + "/materials", status_code=202, tags=["Materials"])
def add_material(app: App, space_id: Id, body: JsonBody):
    return app.add_material(space_id, body)


@router.post(SPACE + "/materials/upload", status_code=202, tags=["Materials"],
             openapi_extra={"requestBody": {"required": True, "content": {"application/octet-stream": {"schema": {"type": "string", "format": "binary"}}}}})
async def upload(request: Request, app: App, space_id: Id, filename: str = "", conversation_id: str = "", model_id: str = "default"):
    data = await request.body()  # HTTPPolicy bounds the raw body before this route.
    def save():
        app.store.history(space_id, conversation_id, 1)
        validate_model_id(model_id)
        item, duplicate = app.library.import_bytes(space_id, filename, data)
        job = app.add_material(space_id, {"conversation_id": conversation_id, "artifact_id": item["id"], "model_id": model_id, "topic": "导入资料"})
        return {"item": item, "duplicate": duplicate, "job": job}
    return await run_in_threadpool(save)


@router.get(SPACE + "/materials/{artifact_id}", tags=["Materials"])
@router.get(SPACE + "/materials/{artifact_id}/original", tags=["Materials"])
@router.get(SPACE + "/materials/{artifact_id}/analysis.md", tags=["Materials"])
@router.get(SPACE + "/materials/{artifact_id}/reader", tags=["Materials"])
@router.get(SPACE + "/materials/{artifact_id}/chunks", tags=["Materials"])
def material(request: Request, app: App, space_id: Id, artifact_id: Id):
    action = request.url.path.rsplit("/", 1)[-1]
    item = app.library.get(space_id, artifact_id)
    if not app.library.authorized(item):
        raise PermissionError("资料文件不在当前授权目录或已被移动，请重新导入")
    if action == "original":
        file = app.library.file(space_id, artifact_id)
        media = "application/pdf" if file.suffix.lower() == ".pdf" else "application/zip" if file.suffix.lower() == ".zip" else "text/plain"
        return respond(200, file.read_bytes(), media, "source.zip" if file.suffix.lower() == ".zip" else None)
    if action == "analysis.md":
        return respond(200, item["analysis"].encode("utf-8"), "text/markdown", "analysis.md")
    chunks = app.library.chunks(space_id, artifact_id)
    if action == "reader":
        introduction = "## 资料信息\n\n" + "\n\n".join(k + "：" + str(v) for k, v in item["metadata"].items()) + "\n\n" + item["boundary"]
        if item["warnings"]:
            introduction += "\n\n" + "\n".join("- " + x for x in item["warnings"])
        if item["original_path"]:
            introduction += f"\n\n[打开原文](/api/spaces/{space_id}/materials/{artifact_id}/original)\n\n文件：" + item["original_path"]
        page = render_report(introduction + "\n\n" + (item["analysis"] or "资料已保存，等待后台解析和分析。") + "\n\n## 已提取的原文", item["title"])
        excerpts = []
        for chunk in chunks:
            label = (f'第 {chunk["page"]} 页' if chunk["page"] else f'第 {chunk["line_start"]}–{chunk["line_end"]} 行') + " · " + chunk["section"]
            excerpts.append(f'<section id="local-L{chunk["id"]}" style="scroll-margin-top:24px"><h3>[L{chunk["id"]}] {html.escape(label)}</h3><p style="white-space:pre-wrap;font-size:19px">{html.escape(chunk["content"])}</p></section>')
        return respond(200, page.replace("</div><footer>", "".join(excerpts) + "</div><footer>", 1).encode("utf-8"), "text/html")
    return {"material": item, "chunks": chunks} if action == "chunks" else item


@router.get(SPACE + "/jobs", tags=["Jobs"])
def jobs(app: App, space_id: Id):
    return {"jobs": app.store.jobs(space_id)}


@router.post(SPACE + "/auto-research", status_code=202, tags=["Auto Research"])
def auto_research(request: Request, app: App, space_id: Id, body: AutoResearchBody):
    if not http.local_desktop(request):
        raise PermissionError("自动研究仅支持本机Windows工作台")
    return app.auto_research.enqueue(space_id, body.conversation_id, body.goal,
                                    model_id=body.model_id, budget=body.budget, targets=body.targets)


@router.get(SPACE + "/auto-research/{job_id}", tags=["Auto Research"])
def auto_detail(app: App, space_id: Id, job_id: Id):
    return app.auto_research.detail(space_id, job_id)


@router.post(JOB + "/cancel", tags=["Jobs"])
def cancel(app: App, space_id: Id, job_id: Id, body: JsonBody):
    return app.store.cancel(space_id, job_id)


@router.post(JOB + "/resume", status_code=202, tags=["Jobs"])
@router.post(JOB + "/retry", status_code=202, tags=["Jobs"])
def restart_job(request: Request, app: App, space_id: Id, job_id: Id, body: JsonBody):
    if app.store.job(space_id, job_id)["kind"] in {"EXPERIMENT", "AUTO_RESEARCH"} and not http.local_desktop(request):
        raise PermissionError("代码执行仅支持本机 Windows 工作台")
    action = app.resume if request.url.path.endswith("/resume") else app.retry
    return action(space_id, job_id)


@router.get(JOB, tags=["Jobs"])
@router.get(JOB + "/local-evidence", tags=["Jobs"])
@router.get(JOB + "/run", tags=["Jobs"])
@router.get(JOB + "/trace", tags=["Jobs"])
@router.get(JOB + "/events", tags=["Jobs"])
@router.get(JOB + "/report.md", tags=["Jobs"])
@router.get(JOB + "/report.html", tags=["Jobs"])
def job(request: Request, app: App, space_id: Id, job_id: Id, after: str = "0"):
    item = app.store.job(space_id, job_id)
    action = request.url.path.rsplit("/", 1)[-1]
    if action == "local-evidence":
        with closing(app.store._connect()) as db:
            records = [dict(r) for r in db.execute("""SELECT c.chunk_id,c.quote,d.page,d.section,a.id artifact_id,a.title,a.original_path,a.space_id,s.name source_space_name FROM citations c
                JOIN document_chunks d ON d.id=c.chunk_id JOIN artifacts a ON a.id=d.artifact_id JOIN research_spaces s ON s.id=a.space_id WHERE c.job_id=? AND s.deleted_at IS NULL""", (job_id,))]
        return {"citations": records, "materials": [m for m in app.library.list(space_id) if m["job_id"] == job_id]}
    if action == "run":
        run = app.store.get(item["run_id"]) if item["run_id"] else None
        if not run:
            raise NotFound("任务尚无研究结果")
        return run
    if action == "trace":
        return http.trace_file(request, item["trace_path"])
    if action == "events":
        return http.trace_events(request, item, after)
    if action.startswith("report."):
        ext = action.split(".")[1]
        if item.get("kind", "RESEARCH") != "RESEARCH":
            if not item["summary"]:
                raise NotFound("任务尚无报告")
            content = item["summary"]
        else:
            app.report_file(space_id, job_id, ext)
            content = app.report_file(space_id, job_id, "md").read_text(encoding="utf-8")
        if ext == "html":
            return respond(200, render_report(content, item["question"]).encode("utf-8"), "text/html")
        return respond(200, content.encode("utf-8"), "text/markdown", "report.md")
    return item


@router.get("/api/runs", tags=["Legacy"])
def runs(app: App):
    return {"runs": app.store.list_runs()}


@router.get("/api/runs/{run_id}", tags=["Legacy"])
@router.get("/api/traces/{run_id}", tags=["Legacy"])
def run(request: Request, app: App, run_id: str):
    item = app.store.get(run_id)
    if not item:
        raise NotFound("run not found")
    return http.trace_file(request, item["trace_path"]) if request.url.path.startswith("/api/traces/") else item


@router.post("/research", tags=["Legacy"])
@router.post("/api/research", tags=["Legacy"])
def research(app: App, body: ResearchBody):
    question = text_field(body.question, "问题", 4000)
    result = app.agent_factory(None).run(question)
    return app.store.get(app.store.save(result, question, now_iso()))
