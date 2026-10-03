"""Single-user research workbench; stdlib HTTP, SQLite and one background worker."""
from __future__ import annotations

import json
import html
import os
import re
import time
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from main import build_search
from research_agent import FixtureReader, HttpReader, OfflineModel, ResearchAgent, load_dotenv, model_from_env
from research_agent.trace import now_iso, redact
from research_agent.folders import choose_folder
from research_agent.models import model_options
from research_agent.reports import render_report
from research_agent.materials import MAX_FILE_BYTES
from research_agent.workbench import OfflineRouter, Workbench
from research_agent.workbench_store import Conflict, NotFound, WorkbenchStore, text_field

ROOT = Path(__file__).resolve().parent


def offline_mode():
    return os.getenv("OFFLINE_MODE", "").strip().lower() in {"1", "true", "yes", "on"}


def live_model(model_id="default"):
    model = model_from_env(model_id)
    if isinstance(model, OfflineModel) and not offline_mode():
        raise ValueError("请配置模型凭据，或显式设置 OFFLINE_MODE=1 使用演示")
    return model


class WorkbenchServer(ThreadingHTTPServer):
    allow_reuse_address = os.name != "nt"

    def lock_database(self, path):
        self.database_lock = open(str(path) + ".lock", "a+b")
        try:
            if self.database_lock.tell() == 0:
                self.database_lock.write(b"0")
                self.database_lock.flush()
            self.database_lock.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.database_lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.database_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.database_lock.close()
            raise RuntimeError("此数据库已有工作台服务运行，请使用原服务或独立数据库") from exc

    def server_close(self):
        if hasattr(self, "app"):
            self.app.close()
        if hasattr(self, "database_lock"):
            self.database_lock.close()
        super().server_close()


def make_server(host="127.0.0.1", port=8000, *, db_path=None, trace_dir=None, model_factory=None, agent_factory=None, start_worker=True):
    load_dotenv(ROOT / ".env")
    store = WorkbenchStore(db_path or os.getenv("DB_PATH", str(ROOT / "data" / "evidence_agent.db")))
    trace_dir = Path(trace_dir or ROOT / "traces").resolve()

    def default_model(model_id="default"):
        return OfflineRouter() if offline_mode() and model_id == "default" else live_model(model_id)

    def default_agent(observer, model_id="default"):
        model = live_model(model_id)
        offline = offline_mode()
        if not offline and not any(os.getenv(k) for k in ("TAVILY_API_KEY", "SEARCH_API_KEY", "SEARCH_URL")):
            raise ValueError("请配置真实搜索服务，或显式启用离线演示")
        reader = FixtureReader.from_file(ROOT / "fixtures" / "search_results.json") if offline else HttpReader()
        return ResearchAgent(build_search(), model, trace_dir=trace_dir, reader=reader, max_tool_calls=16, max_rounds=20, on_event=observer)

    server = WorkbenchServer((host, port), Handler)
    try:
        server.lock_database(store.path.resolve())
        app = Workbench(store, model_factory or default_model, agent_factory or default_agent, trace_dir, start_worker=False)
    except Exception:
        server.server_close()
        raise
    server.app = app
    server.daemon_threads = True
    if start_worker:
        app.coding.start()
        app.worker.start()
    return server


class Handler(BaseHTTPRequestHandler):
    def respond(self, status, data, content_type="application/json; charset=utf-8", attachment=None):
        if not isinstance(data, bytes):
            data = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if attachment:
            self.send_header("Content-Disposition", f'attachment; filename="{attachment}"')
        self.end_headers()
        self.wfile.write(data)

    def check_origin(self):
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            raise PermissionError("不接受跨站写入请求")
        origin = self.headers.get("Origin")
        if origin and (urlparse(origin).netloc != self.headers.get("Host") or urlparse(origin).scheme not in {"http", "https"}):
            raise PermissionError("请求来源不匹配")

    def body(self):
        self.check_origin()
        if self.headers.get_content_type() != "application/json":
            raise ValueError("请求必须使用 application/json")
        size = int(self.headers.get("Content-Length", "0"))
        if not 1 <= size <= 100000:
            raise ValueError("请求体大小不合法")
        body = json.loads(self.rfile.read(size))
        if not isinstance(body, dict):
            raise ValueError("请求体必须为 JSON 对象")
        return body

    def handle_api(self):
        app, method = self.server.app, self.command
        store = app.store
        path = unquote(urlparse(self.path).path)
        upload=re.fullmatch(r'/api/spaces/([a-f0-9]{32})/materials/upload',path)
        if method=='POST' and upload:
            self.check_origin()
            if self.headers.get_content_type()!='application/octet-stream' or not self.headers.get('Origin'):
                raise ValueError('请通过资料库的文件选择按钮导入资料')
            size=int(self.headers.get('Content-Length','0'))
            if not 0<size<=MAX_FILE_BYTES:
                raise ValueError('导入文件应在 25 MB 以内')
            params=parse_qs(urlparse(self.path).query)
            filename=params.get('filename',[''])[0]
            conversation=params.get('conversation_id',[''])[0]
            model_id=params.get('model_id',['default'])[0]
            store.history(upload[1],conversation,1)
            from research_agent.models import validate_model_id
            validate_model_id(model_id)
            data=self.rfile.read(size)
            if len(data)!=size:
                raise ValueError('文件上传不完整，未保存')
            item,duplicate=app.library.import_bytes(upload[1],filename,data)
            job=app.add_material(upload[1],{'conversation_id':conversation,'artifact_id':item['id'],'model_id':model_id,'topic':'导入资料'})
            return self.respond(202,{'item':item,'duplicate':duplicate,'job':job})
        body = self.body() if method in {"POST", "PATCH", "DELETE"} else {}
        if method == "GET" and path == "/":
            return self.respond(200, (ROOT / "web" / "index.html").read_bytes(), "text/html; charset=utf-8")
        if method == 'GET' and path.startswith(('/evaluation/','/v3-evaluation/','/harness-evaluation/','/v4-evaluation/','/evidence-qa-evaluation/')):
            prefix=next(p for p in ('/evaluation/','/v3-evaluation/','/harness-evaluation/','/v4-evaluation/','/evidence-qa-evaluation/') if path.startswith(p))
            report_dirs={'/evaluation/':'context-memory-optimization-20260918','/v3-evaluation/':'roadmap-v3-20260921','/harness-evaluation/':'harness-strategies-20260921','/v4-evaluation/':'v4-first-cycle-20260923','/evidence-qa-evaluation/':'evidence-qa-20260927'}
            directory=(ROOT/'evals/reports'/report_dirs[prefix]).resolve()
            file=(directory/path.removeprefix(prefix)).resolve()
            media={'.html':'text/html; charset=utf-8','.md':'text/markdown; charset=utf-8','.json':'application/json; charset=utf-8','.jsonl':'application/x-ndjson; charset=utf-8','.log':'text/plain; charset=utf-8','.patch':'text/plain; charset=utf-8','.py':'text/plain; charset=utf-8'}
            if not file.is_relative_to(directory) or file.suffix not in media or not file.is_file():
                raise NotFound('评测文件不存在')
            return self.respond(200,file.read_bytes(),media[file.suffix])
        if method == "GET" and path in {"/style.css", "/app.js", "/research-records.js", "/strategies.js", "/highlight.min.js"}:
            return self.respond(200, (ROOT / "web" / path[1:]).read_bytes(), "text/css; charset=utf-8" if path.endswith(".css") else "text/javascript; charset=utf-8")
        if method == "GET" and path == "/assistant.svg":
            return self.respond(200, (ROOT / "web" / "assistant.svg").read_bytes(), "image/svg+xml")
        if method == "GET" and path == "/health":
            return self.respond(200, {"status": "ok", "version": "v4", "mode": "offline-demo" if offline_mode() else "live", "folder_picker": self.local_desktop()})
        if method == "GET" and path == "/api/models":
            return self.respond(200, model_options())
        if method == "POST" and path == "/api/folders/pick":
            if not self.local_desktop() or not self.headers.get("Origin"):
                raise PermissionError("目录选择仅支持从本机 Windows 的工作台页面打开")
            return self.respond(200, choose_folder(body.get("initial", "")))
        if path == "/api/spaces":
            if method == "GET":
                return self.respond(200, {"spaces": store.spaces(parse_qs(urlparse(self.path).query).get('state',['active'])[0])})
            if method == "POST":
                return self.respond(201, store.save_space(body))
        match = re.fullmatch(r"/api/spaces/([a-f0-9]{32})(.*)", path)
        if match:
            space_id, rest = match.groups()
            if rest.startswith('/strategies'):
                policy=app.strategies;store.space(space_id)
                if rest=='/strategies' and method=='GET':return self.respond(200,policy.overview(space_id))
                if rest=='/strategies/feedback' and method=='POST':return self.respond(201,policy.feedback(space_id,body.get('job_id'),body.get('kind'),body.get('note')))
                if rest=='/strategies/propose' and method=='POST':return self.respond(201,policy.propose(space_id,body.get('feedback_id')))
                if rest=='/strategies/activate' and method=='POST':return self.respond(200,policy.activate(space_id,body.get('version_id'),body.get('evaluation_id')))
                if rest=='/strategies/rollback' and method=='POST':return self.respond(200,policy.rollback(space_id,body.get('reason','用户回退')))
                raise NotFound('策略接口不存在')
            if rest.startswith('/research-records'):
                return self.research_records_api(space_id, rest.removeprefix('/research-records'), method, body)
            recalled=re.fullmatch(r'/(history|memory)/([a-f0-9]{32})',rest)
            if recalled and method=='GET':
                corpus,item_id=recalled.groups()
                origin=store.space(space_id)
                if corpus=='history':
                    history=store.history(space_id,item_id)
                    content='\n\n'.join('## '+('用户提问' if m['role']=='user' else '助手回答')+'\n\n'+m['content'] for m in history if m['intent']!='PENDING')
                else:
                    item=next((m for m in app.memory.list(space_id) if m['id']==item_id),None)
                    if item is None:raise NotFound('记忆不存在或已删除')
                    content=item['content']+'\n\n## 来源\n\n'+'\n\n'.join(str(r.get('quote','')) for r in item['source_refs'])
                return self.respond(200,render_report('来源研究区：'+origin['name']+'\n\n历史内容仅供回溯，不代表当前事实核验。\n\n'+content,'研究历史 · '+origin['name']).encode('utf-8'),'text/html; charset=utf-8')
            if rest in {'/archive','/restore'} and method=='POST':
                return self.respond(200,store.set_space_state(space_id,'archived' if rest=='/archive' else 'active'))
            if rest=='/recycle' and method=='GET':
                store.space(space_id)
                with closing(store._connect()) as db:
                    turns=[dict(r) for r in db.execute('''SELECT m.conversation_id,m.turn_seq,c.title,min(m.id) id,
                        max(CASE WHEN m.role='user' THEN m.content ELSE '' END) content
                        FROM messages m JOIN conversations c ON c.id=m.conversation_id
                        WHERE c.space_id=? AND c.deleted_at IS NULL AND m.deleted_at IS NOT NULL
                        GROUP BY m.conversation_id,m.turn_seq ORDER BY id DESC''',(space_id,))]
                conversations=store.conversations(space_id,True)
                return self.respond(200,{'branches':[b for b in conversations if b['deleted_at']],
                    'archived':[b for b in conversations if b['archived_at'] and not b['deleted_at']], 'turns':turns})
            turn=re.fullmatch(r'/conversations/([a-f0-9]{32})/(messages|turns)/(\d+)(/restore)?',rest)
            if turn:
                branch,kind,number,restore=turn.groups()
                if method=='DELETE' and kind=='messages' and not restore:
                    return self.respond(200,app.sessions.delete_turn(space_id,branch,int(number)))
                if method=='POST' and kind=='turns' and restore:
                    return self.respond(200,app.sessions.restore_turn(space_id,branch,int(number)))
            stop=re.fullmatch(r'/conversations/([a-f0-9]{32})/stop',rest)
            if stop and method=='POST':
                store.history(space_id,stop[1],1)
                stopped=[]
                for item in store.jobs(space_id):
                    if item['conversation_id']==stop[1] and item['status'] in {'queued','running'}:
                        try:stopped.append(store.cancel(space_id,item['id'])['id'])
                        except Conflict:pass  # Completion can win the race with the stop button.
                return self.respond(200,{'stopped':stopped})
            if rest=='/materials':
                if method=='GET':
                    app.library.sync_reports(space_id)
                    return self.respond(200,{'materials':app.library.list(space_id)})
                if method=='POST':
                    return self.respond(202,app.add_material(space_id,body))
            material=re.fullmatch(r'/materials/([a-f0-9]{32})(?:/(reader|original|analysis.md|chunks))?',rest)
            if material and method=='GET':
                artifact_id,action=material.groups()
                item=app.library.get(space_id,artifact_id)
                if not app.library.authorized(item):
                    raise PermissionError('资料文件不在当前授权目录或已被移动，请重新导入')
                if action=='original':
                    file=app.library.file(space_id,artifact_id)
                    media='application/pdf' if file.suffix.lower()=='.pdf' else 'application/zip' if file.suffix.lower()=='.zip' else 'text/plain; charset=utf-8'
                    return self.respond(200,file.read_bytes(),media, 'source.zip' if file.suffix.lower()=='.zip' else None)
                if action=='analysis.md':
                    return self.respond(200,item['analysis'].encode('utf-8'),'text/markdown; charset=utf-8','analysis.md')
                chunks=app.library.chunks(space_id,artifact_id)
                if action=='reader':
                    metadata=item['metadata']
                    introduction='## 资料信息\n\n'+ '\n\n'.join(k+'：'+str(v) for k,v in metadata.items())+'\n\n'+item['boundary']
                    if item['warnings']:
                        introduction+='\n\n'+'\n'.join('- '+x for x in item['warnings'])
                    if item['original_path']:
                        introduction+=f'\n\n[打开原文](/api/spaces/{space_id}/materials/{artifact_id}/original)\n\n文件：'+item['original_path']
                    page=render_report(introduction+'\n\n'+(item['analysis'] or '资料已保存，等待后台解析和分析。')+'\n\n## 已提取的原文',item['title'])
                    excerpts=[]
                    for chunk in chunks:
                        label=(f'第 {chunk["page"]} 页' if chunk['page'] else f'第 {chunk["line_start"]}–{chunk["line_end"]} 行')+' · '+chunk['section']
                        excerpts.append(f'<section id="local-L{chunk["id"]}" style="scroll-margin-top:24px"><h3>[L{chunk["id"]}] {html.escape(label)}</h3><p style="white-space:pre-wrap;font-size:19px">{html.escape(chunk["content"])}</p></section>')
                    page=page.replace('</div><footer>',''.join(excerpts)+'</div><footer>',1)
                    return self.respond(200,page.encode('utf-8'),'text/html; charset=utf-8')
                return self.respond(200,{'material':item,'chunks':chunks} if action=='chunks' else item)
            if not rest:
                if method == "GET":
                    return self.respond(200, store.space(space_id))
                if method == "PATCH":
                    return self.respond(200, store.save_space(body, space_id))
                if method == "DELETE":
                    if not app.routing.acquire(blocking=False):
                        raise Conflict("正在处理消息，请稍后删除研究区")
                    try:
                        store.delete_space(space_id)
                    finally:
                        app.routing.release()
                    return self.respond(200, {"deleted": True})
            if rest == '/retrieval' and method == 'GET':
                store.space(space_id)
                return self.respond(200,app.retrieval.status(space_id))
            if rest == '/memories' and method == 'GET':
                return self.respond(200,{'memories':app.memory.list(space_id,include_global=True)})
            memory=re.fullmatch(r'/memories/([a-f0-9]{32})',rest)
            if memory:
                if method=='PATCH':
                    return self.respond(200,app.memory.update(space_id,memory[1],body))
                if method=='DELETE':
                    return self.respond(200,app.memory.forget(space_id,memory[1]))
            session=re.fullmatch(r'/conversations/([a-f0-9]{32})(?:/(fork|context|events|restore))?',rest)
            if session:
                conversation_id,action=session.groups()
                if action=='restore' and method=='POST':
                    return self.respond(200,app.sessions.restore_branch(space_id,conversation_id))
                if not action and method=='DELETE':
                    return self.respond(200,app.sessions.delete_branch(space_id,conversation_id))
                if action=='events' and method=='GET':
                    return self.conversation_stream(space_id,conversation_id)
                if action=='fork' and method=='POST':
                    return self.respond(201,app.sessions.fork(space_id,conversation_id,body.get('title'),body.get('through_message_id')))
                if action=='context' and method=='GET':
                    return self.respond(200,app.sessions.context(space_id,conversation_id))
                if not action and method=='PATCH':
                    return self.respond(200,app.sessions.update(space_id,conversation_id,body))
            if rest == "/conversations":
                if method == "GET":
                    return self.respond(200, {"conversations": store.conversations(space_id)})
                if method == "POST":
                    return self.respond(201, store.create_conversation(space_id, body.get("title", "新会话")))
            conversation = re.fullmatch(r"/conversations/([a-f0-9]{32})/messages", rest)
            if conversation:
                if method == "GET":
                    return self.respond(200, {"messages": store.history(space_id, conversation[1])})
                if method == "POST":
                    background=body.get('background',False)
                    if type(background) is not bool:
                        raise ValueError('background 必须为布尔值')
                    result = app.send(space_id, conversation[1], body.get("content"), body.get("search_scope", "auto"), body.get("model_id", "default"),background=background,allow_execution=self.local_desktop())
                    return self.respond(202 if result.get('task_id') else 200, result)
            if rest == "/jobs" and method == "GET":
                return self.respond(200, {"jobs": store.jobs(space_id)})
            if rest == '/auto-research' and method == 'POST':
                if not self.local_desktop():raise PermissionError('自动研究仅支持本机Windows工作台')
                if set(body)-{'conversation_id','goal','model_id','budget','targets'}:raise ValueError('未知自动研究字段')
                return self.respond(202, app.auto_research.enqueue(space_id, body.get('conversation_id'), body.get('goal'),
                    model_id=body.get('model_id','default'), budget=body.get('budget'), targets=body.get('targets')))
            auto = re.fullmatch(r'/auto-research/([a-f0-9]{32})', rest)
            if auto and method == 'GET':
                return self.respond(200, app.auto_research.detail(space_id, auto[1]))
            job = re.fullmatch(r"/jobs/([a-f0-9]{32})(?:/(cancel|retry|resume|run|trace|events|local-evidence|report\.md|report\.html))?", rest)
            if job:
                job_id, action = job.groups()
                if method == "POST" and action == "cancel":
                    return self.respond(200, store.cancel(space_id, job_id))
                if method == "POST" and action == "resume":
                    if store.job(space_id,job_id)['kind'] in {'EXPERIMENT','AUTO_RESEARCH'} and not self.local_desktop():raise PermissionError('代码执行仅支持本机 Windows 工作台')
                    return self.respond(202,app.resume(space_id,job_id))
                if method == "POST" and action == "retry":
                    if store.job(space_id,job_id)['kind'] in {'EXPERIMENT','AUTO_RESEARCH'} and not self.local_desktop():raise PermissionError('代码执行仅支持本机 Windows 工作台')
                    return self.respond(202, app.retry(space_id, job_id))
                if method == "GET":
                    item = store.job(space_id, job_id)
                    if not action:
                        return self.respond(200, item)
                    if action=='local-evidence':
                        with closing(store._connect()) as db:
                            records=[dict(row) for row in db.execute('''SELECT c.chunk_id,c.quote,d.page,d.section,a.id artifact_id,a.title,a.original_path,a.space_id,s.name source_space_name FROM citations c
                                JOIN document_chunks d ON d.id=c.chunk_id JOIN artifacts a ON a.id=d.artifact_id JOIN research_spaces s ON s.id=a.space_id WHERE c.job_id=? AND s.deleted_at IS NULL''',(job_id,))]
                        return self.respond(200,{'citations':records,'materials':[m for m in app.library.list(space_id) if m['job_id']==job_id]})
                    if action == "run":
                        run = store.get(item["run_id"]) if item["run_id"] else None
                        if not run:
                            raise NotFound("任务尚无研究结果")
                        return self.respond(200, run)
                    if action == "trace":
                        return self.trace_file(item["trace_path"])
                    if action == "events":
                        return self.trace_events(item)
                    if action.startswith("report."):
                        ext = action.split(".")[1]
                        if item.get('kind','RESEARCH')!='RESEARCH':
                            if not item['summary']:
                                raise NotFound('任务尚无报告')
                            content=render_report(item['summary'],item['question']) if ext=='html' else item['summary']
                            return self.respond(200,content.encode('utf-8'),'text/html; charset=utf-8' if ext=='html' else 'text/markdown; charset=utf-8',None if ext=='html' else 'report.md')
                        file = app.report_file(space_id, job_id, ext)
                        if ext == "html":
                            # Render saved Markdown so historical reports gain the reader without rewriting the archive.
                            content = app.report_file(space_id, job_id, "md").read_text(encoding="utf-8")
                            return self.respond(200, render_report(content, item["question"]).encode("utf-8"), "text/html; charset=utf-8")
                        return self.respond(200, file.read_bytes(), "text/html; charset=utf-8" if ext == "html" else "text/markdown; charset=utf-8", "report.md" if ext == "md" else None)
        # Retain the original Stage 7 API for existing clients and historical runs.
        if method == "GET" and path == "/api/runs":
            return self.respond(200, {"runs": store.list_runs()})
        legacy = re.fullmatch(r"/api/(runs|traces)/([^/]+)", path)
        if method == "GET" and legacy:
            run = store.get(legacy[2])
            if not run:
                raise NotFound("run not found")
            if legacy[1] == "traces":
                return self.trace_file(run["trace_path"])
            return self.respond(200, run)
        if method == "POST" and path in {"/research", "/api/research"}:
            question = text_field(body.get("question"), "问题", 4000)
            result = app.agent_factory(None).run(question)
            return self.respond(200, store.get(store.save(result, question, now_iso())))
        raise NotFound("接口不存在")

    def research_records_api(self, space_id, rest, method, body):
        records=self.server.app.records
        self.server.app.store.space(space_id)
        params=parse_qs(urlparse(self.path).query)
        if rest=='/reports' and method=='GET':
            return self.respond(200,{'reports':records.reports(space_id)})
        report=re.fullmatch(r'/reports/([a-f0-9]{32})(?:/(versions|compare))?',rest)
        if report:
            report_id,action=report.groups()
            if not action and method=='GET':
                item=records.report(space_id,report_id)
                for version in item['versions']:version['coverage']=records.coverage(version)
                return self.respond(200,item)
            if action=='versions' and method=='POST':
                item,version_id=records.draft(space_id,report_id,body)
                return self.respond(201,{'report':item,'version_id':version_id})
            if action=='compare' and method=='GET':
                return self.respond(200,records.compare_reports(space_id,report_id,params.get('left',[''])[0],params.get('right',[''])[0]))
        version=re.fullmatch(r'/reports/([a-f0-9]{32})/versions/([a-f0-9]{32})(?:/(report\.md|report\.html))?',rest)
        if version:
            report_id,version_id,action=version.groups()
            if not action and method=='PATCH':
                return self.respond(200,records.transition(space_id,report_id,version_id,body))
            if action and method=='GET':
                content=records.report_markdown(space_id,report_id,version_id)
                if action.endswith('.html'):
                    return self.respond(200,render_report(content,'研究报告 · 版本档案').encode('utf-8'),'text/html; charset=utf-8')
                return self.respond(200,content.encode('utf-8'),'text/markdown; charset=utf-8','report.md')
        if rest=='/graph' and method=='GET':return self.respond(200,records.graph(space_id))
        if rest=='/entities' and method=='POST':return self.respond(201,records.entity(space_id,body))
        if rest=='/relations' and method=='POST':return self.respond(201,records.relation(space_id,body))
        if rest=='/ideas' and method=='GET':return self.respond(200,{'ideas':records.ideas(space_id)})
        if rest=='/experiments':
            if method=='GET':return self.respond(200,{'experiments':records.experiments(space_id)})
            if method=='POST':return self.respond(201,records.save_experiment(space_id,body))
        execution=self.server.app.experiments
        if rest=='/execution-plans' and method=='POST':return self.respond(201,execution.prepare(space_id,body))
        if rest=='/executions' and method=='GET':return self.respond(200,{'executions':execution.list(space_id)})
        execute=re.fullmatch(r'/experiment-versions/([a-f0-9]{32})/execute',rest)
        if execute and method=='POST':
            if not self.local_desktop():raise PermissionError('代码执行仅支持本机 Windows 工作台')
            return self.respond(202,execution.submit(space_id,execute[1],body))
        artifact=re.fullmatch(r'/executions/([a-f0-9]{32})/artifacts/([a-z.-]+)',rest)
        if artifact and method=='GET':
            file=execution.artifact(space_id,artifact[1],artifact[2])
            return self.respond(200,file.read_bytes(),'text/plain; charset=utf-8',file.name)
        run=re.fullmatch(r'/executions/([a-f0-9]{32})',rest)
        if run and method=='GET':return self.respond(200,execution.get(space_id,run[1]))
        experiment=re.fullmatch(r'/experiments/([a-f0-9]{32})(/versions)?',rest)
        if experiment:
            if method=='GET' and not experiment[2]:return self.respond(200,records.experiment(space_id,experiment[1]))
            if method=='POST' and experiment[2]:return self.respond(201,records.save_experiment(space_id,body,experiment[1]))
        if rest=='/experiment-comparison' and method=='GET':
            return self.respond(200,records.compare_experiments(space_id,params.get('left',[''])[0],params.get('right',[''])[0]))
        handoff=re.fullmatch(r'/experiment-versions/([a-f0-9]{32})/handoff',rest)
        if handoff and method=='GET':return self.respond(200,records.handoff(space_id,handoff[1]))
        raise NotFound('研究成果接口不存在')

    def local_desktop(self):
        hostname = urlparse("http://" + self.headers.get("Host", "")).hostname
        return os.name == "nt" and hostname in {"localhost", "127.0.0.1", "::1"} and self.client_address[0] in {"127.0.0.1", "::1"} and self.server.server_address[0] in {"127.0.0.1", "::1"}

    def trace_path(self, raw):
        path = Path(raw)
        path = (path if path.is_absolute() else ROOT / path).resolve()
        if not path.is_relative_to(self.server.app.trace_dir) or path.suffix != ".jsonl" or not path.is_file():
            raise NotFound("Trace 不存在或不在允许目录")
        return path

    def trace_file(self, raw):
        return self.respond(200, self.trace_path(raw).read_bytes(), "application/x-ndjson; charset=utf-8", "trace.jsonl")

    def trace_events(self, job):
        cursor = parse_qs(urlparse(self.path).query, keep_blank_values=True).get("after", ["0"])[0]
        if not re.fullmatch(r"[0-9]{1,10}", cursor):
            raise ValueError("事件游标必须为非负整数")
        after, events = int(cursor), []
        if job["trace_path"]:
            with self.trace_path(job["trace_path"]).open(encoding="utf-8") as stream:
                for line in stream:
                    if not line.endswith("\n"):
                        break  # A writer may still be completing the last event.
                    record = json.loads(line)
                    if record.get("seq", 0) > after:
                        events.append(record)
                        if len(events) == 200:
                            break
        return self.respond(200, {"events": events, "next_seq": events[-1]["seq"] if events else after, "status": job["status"]})

    def conversation_stream(self, space_id, conversation_id):
        app=self.server.app
        app.store.history(space_id,conversation_id,1)  # Enforce scope before sending headers.
        self.check_origin()
        cursor=self.headers.get('Last-Event-ID') or parse_qs(urlparse(self.path).query).get('after',['0'])[0]
        if not re.fullmatch(r'[0-9]{1,18}',cursor):
            raise ValueError('事件游标必须为非负整数')
        after=int(cursor)
        kinds={'model_stream','answer_draft','model_request','tool_call_requested','tool_result',
               'answer_check_started','answer_checked','answer_repair','answer_check_unavailable','context_compacted',
               'material_progress','material_failed','model_error','run_finished','message'}
        with closing(app.store._connect()) as db:
            if not after:
                first=db.execute("SELECT min(e.id) FROM conversation_events e JOIN research_jobs j ON j.id=e.job_id WHERE e.conversation_id=? AND j.status IN ('running','queued')",(conversation_id,)).fetchone()[0]
                after=(first-1) if first else db.execute('SELECT coalesce(max(id),0) FROM conversation_events WHERE conversation_id=?',(conversation_id,)).fetchone()[0]
        self.send_response(200)
        self.send_header('Content-Type','text/event-stream; charset=utf-8')
        self.send_header('Cache-Control','no-cache, no-transform')
        self.send_header('X-Accel-Buffering','no')
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Connection','close')
        self.end_headers()
        self.close_connection=True

        def emit(event, payload, seq=None):
            block=(f'id: {seq}\n' if seq is not None else '')+'event: '+event+'\ndata: '+json.dumps(payload,ensure_ascii=False)+'\n\n'
            self.wfile.write(block.encode('utf-8'));self.wfile.flush()

        previous=None
        until=time.monotonic()+45
        heartbeat=time.monotonic()
        while not app.stop.is_set() and time.monotonic()<until:
            with closing(app.store._connect()) as db:
                jobs=[dict(r) for r in db.execute("SELECT id,status,stage,recent_action,model_name FROM research_jobs WHERE conversation_id=? AND status IN ('running','queued') ORDER BY created_at,turn_seq,id",(conversation_id,))]
                rows=db.execute('SELECT id,job_id,kind,payload,created_at FROM conversation_events WHERE conversation_id=? AND id>? ORDER BY id LIMIT 200',(conversation_id,after)).fetchall()
            if jobs!=previous:
                emit('jobs',{'jobs':jobs});previous=jobs
            for row in rows:
                after=row['id']
                if row['kind'] in kinds:
                    payload=json.loads(row['payload'])
                    # Tool results can be large; expose status, not entire untrusted documents.
                    if row['kind']=='tool_result':
                        payload={k:payload[k] for k in ('name','ok','title','error') if k in payload}
                    elif row['kind']=='answer_checked':
                        payload={'passed':payload.get('passed'), 'state':payload.get('state')}
                    emit('progress',{'job_id':row['job_id'],'kind':row['kind'],'payload':payload,'ts':row['created_at']},after)
            if rows or time.monotonic()-heartbeat>=10:
                emit('cursor',{},after);heartbeat=time.monotonic()
            app.stop.wait(.15 if len(rows)<200 else .01)

    def dispatch(self):
        self.connection.settimeout(60)
        try:
            self.handle_api()
        except (ConnectionError, TimeoutError):
            pass
        except Exception as exc:
            status = 404 if isinstance(exc, (NotFound, FileNotFoundError)) else 409 if isinstance(exc, Conflict) else 403 if isinstance(exc, PermissionError) else 400 if isinstance(exc, (ValueError, UnicodeError)) else 500
            try:
                self.respond(status, {"error": str(redact(str(exc)))[:600]})
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                pass

    do_GET = do_POST = do_PATCH = do_DELETE = dispatch

    def log_message(self, format, *args):
        return


if __name__ == "__main__":
    load_dotenv(ROOT / ".env")
    host, port = os.getenv("HOST", "127.0.0.1"), int(os.getenv("PORT", "8000"))
    server = make_server(host, port)
    print(f"ResearchAgent V2: http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.app.close()
        server.server_close()
