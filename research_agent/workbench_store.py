"""Additive workbench schema; completed legacy runs stay in their original tables."""
from __future__ import annotations

import json
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from .storage import RunStore
from .trace import now_iso
from .models import validate_model_id

RESEARCH_BUDGETS = {"quick": 8, "standard": 16, "deep": 24, "legacy": 6}
RESEARCH_CONTEXT_BUDGETS = {"quick": 16_384, "standard": 32_768, "deep": 65_536, "legacy": 16_384}


class NotFound(ValueError):
    pass


class Conflict(ValueError):
    pass


def text_field(value, name: str, limit: int, *, empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > limit or (not empty and not value.strip()):
        raise ValueError(f"{name} 必须是{'非空' if not empty else ''}文本，最多 {limit} 字符")
    return value.strip()


class WorkbenchStore(RunStore):
    def _init(self) -> None:
        super()._init()
        with closing(self._connect()) as db, db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS research_spaces (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL,
                download_root TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS conversations (
                id TEXT PRIMARY KEY, space_id TEXT NOT NULL REFERENCES research_spaces(id) ON DELETE CASCADE,
                title TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                role TEXT NOT NULL CHECK(role IN ('user','assistant')), content TEXT NOT NULL,
                intent TEXT NOT NULL DEFAULT '', job_id TEXT, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS research_jobs (
                id TEXT PRIMARY KEY, space_id TEXT NOT NULL REFERENCES research_spaces(id) ON DELETE CASCADE,
                conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                question TEXT NOT NULL, brief TEXT NOT NULL, assumptions TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('queued','running','completed','failed','cancelled','interrupted')),
                stage TEXT NOT NULL, recent_action TEXT NOT NULL DEFAULT '', summary TEXT NOT NULL DEFAULT '',
                error TEXT NOT NULL DEFAULT '', run_id TEXT REFERENCES runs(id), trace_path TEXT NOT NULL DEFAULT '',
                retry_of TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS reports (
                id TEXT PRIMARY KEY, job_id TEXT NOT NULL UNIQUE REFERENCES research_jobs(id) ON DELETE CASCADE,
                space_id TEXT NOT NULL REFERENCES research_spaces(id) ON DELETE CASCADE,
                run_id TEXT NOT NULL REFERENCES runs(id), title TEXT NOT NULL,
                markdown_path TEXT NOT NULL, html_path TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS idx_conversations_space ON conversations(space_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(conversation_id, id);
            CREATE INDEX IF NOT EXISTS idx_jobs_space ON research_jobs(space_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_jobs_status ON research_jobs(status, created_at);
            PRAGMA user_version = 1;
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(research_jobs)")}
            if "search_scope" not in columns:
                db.execute("ALTER TABLE research_jobs ADD COLUMN search_scope TEXT NOT NULL DEFAULT 'web'")
            if "research_effort" not in columns:
                db.execute("ALTER TABLE research_jobs ADD COLUMN research_effort TEXT NOT NULL DEFAULT 'legacy'")
            for table in ("research_jobs", "messages"):
                model_columns = {row[1] for row in db.execute("PRAGMA table_info(" + table + ")")}
                if "model_id" not in model_columns:
                    db.execute("ALTER TABLE " + table + " ADD COLUMN model_id TEXT NOT NULL DEFAULT 'default'")
                if "model_name" not in model_columns:
                    db.execute("ALTER TABLE " + table + " ADD COLUMN model_name TEXT NOT NULL DEFAULT ''")
            from .library import initialize
            initialize(db)
            from . import sessions, memory, retrieval
            sessions.initialize(db)
            memory.initialize(db)
            retrieval.initialize(db)
            from .research_records import initialize as initialize_research_records
            initialize_research_records(db)
            from .strategies import initialize as initialize_strategies
            initialize_strategies(db)
            from .experiments import initialize as initialize_experiments
            initialize_experiments(db)
            from .coding_tool import initialize as initialize_coding_tool
            initialize_coding_tool(db)

    @staticmethod
    def _require(db, table, item_id, space_id=None, *, include_deleted=False):
        # Table names only come from internal constants, never HTTP input.
        sql, args = f"SELECT * FROM {table} WHERE id=?", [item_id]
        if space_id is not None:
            sql += " AND space_id=?"
            args.append(space_id)
        row = db.execute(sql, args).fetchone()
        if row is None:
            raise NotFound("记录不存在或不属于当前研究区")
        if not include_deleted:
            if 'deleted_at' in row.keys() and row['deleted_at']:
                raise NotFound('记录已移入回收站')
            parent_space = row['space_id'] if 'space_id' in row.keys() else None
            if parent_space and db.execute('SELECT 1 FROM research_spaces WHERE id=? AND deleted_at IS NOT NULL',(parent_space,)).fetchone():
                raise NotFound('研究区已移入回收站')
            if table == 'research_jobs' and not WorkbenchStore.job_visible(db,row):
                raise NotFound('任务所属分支或对话已移入回收站')
            if table == 'artifacts' and row['kind']=='report' and row['job_id']:
                WorkbenchStore._require(db,'research_jobs',row['job_id'])
        return dict(row)

    @staticmethod
    def job_visible(db,job):
        return bool(db.execute('''SELECT 1 FROM conversations c JOIN research_spaces s ON s.id=c.space_id
            WHERE c.id=? AND c.deleted_at IS NULL AND s.deleted_at IS NULL
            AND NOT EXISTS (SELECT 1 FROM messages m WHERE m.conversation_id=c.id
                AND m.turn_seq=? AND m.deleted_at IS NOT NULL)''',(job['conversation_id'],job['turn_seq'])).fetchone())

    def spaces(self,state='active'):
        if state not in {'active','archived','trash'}:raise ValueError('invalid session state')
        condition={'active':'deleted_at IS NULL AND archived_at IS NULL','archived':'deleted_at IS NULL AND archived_at IS NOT NULL','trash':'deleted_at IS NOT NULL'}[state]
        with closing(self._connect()) as db:
            return [dict(r) for r in db.execute('SELECT * FROM research_spaces WHERE '+condition+' ORDER BY created_at,id')]

    def space(self, space_id):
        with closing(self._connect()) as db:
            return self._require(db, "research_spaces", space_id)

    def save_space(self, body, space_id=None):
        if set(body) - {"name", "description", "download_root", "auto_download", "download_count", "download_mb"}:
            raise ValueError("未知研究区字段")
        old = self.space(space_id) if space_id else {}
        name = text_field(body.get("name", old.get("name")), "名称", 100)
        description = text_field(body.get("description", old.get("description", "")), "描述", 2000, empty=True)
        space_id = space_id or uuid4().hex
        raw = text_field(body.get("download_root", old.get("download_root", str(self.path.resolve().parent / "downloads" / space_id))), "下载根目录", 2000)
        root = Path(raw)
        if not root.is_absolute() or ".." in root.parts or raw.startswith(("\\\\", "//")):
            raise ValueError("下载根目录必须是本机绝对路径，不能包含 .. 或网络共享")
        root = root.resolve()
        if root.exists() and not root.is_dir():
            raise ValueError("下载根目录不能是文件")
        auto_download = body.get('auto_download', old.get('auto_download', 0))
        count = body.get('download_count', old.get('download_count', 3))
        mb = body.get('download_mb', old.get('download_mb', 50))
        if auto_download not in (False, True, 0, 1) or not isinstance(auto_download,(bool,int)):
            raise ValueError('自动下载开关不合法')
        if type(count) is not int or not 1 <= count <= 20 or type(mb) is not int or not 1 <= mb <= 500:
            raise ValueError('下载预算应为 1–20 篇、1–500 MB')
        now = now_iso()
        with closing(self._connect()) as db, db:
            if old:
                self._require(db, "research_spaces", space_id)
                if str(root) != old['download_root'] and db.execute("SELECT 1 FROM research_jobs WHERE space_id=? AND status IN ('queued','running')",(space_id,)).fetchone():
                    raise Conflict('资料任务运行期间不能更改授权目录')
                db.execute("UPDATE research_spaces SET name=?,description=?,download_root=?,updated_at=? WHERE id=?", (name, description, str(root), now, space_id))
            else:
                db.execute("INSERT INTO research_spaces(id,name,description,download_root,created_at,updated_at) VALUES(?,?,?,?,?,?)", (space_id, name, description, str(root), now, now))
            db.execute('UPDATE research_spaces SET auto_download=?,download_count=?,download_mb=? WHERE id=?',(int(auto_download),count,mb,space_id))
        return self.space(space_id)

    def delete_space(self, space_id):
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            self._require(db, "research_spaces", space_id)
            if db.execute("SELECT 1 FROM research_jobs WHERE space_id=? AND status IN ('queued','running')", (space_id,)).fetchone():
                raise Conflict("请先取消该研究区中未完成的任务")
            db.execute('UPDATE research_spaces SET deleted_at=? WHERE id=?',(now_iso(),space_id))
            from .memory import Memory
            Memory.invalidate(db,None)

    def set_space_state(self,space_id,state):
        if state not in {'active','archived'}:raise ValueError('invalid session state')
        with closing(self._connect()) as db,db:
            db.execute('BEGIN IMMEDIATE')
            previous=self._require(db,'research_spaces',space_id,include_deleted=True)
            if db.execute("SELECT 1 FROM research_jobs WHERE space_id=? AND status IN ('queued','running')",(space_id,)).fetchone():
                raise Conflict('请先停止该研究区中未完成的任务')
            db.execute('UPDATE research_spaces SET deleted_at=NULL,archived_at=? WHERE id=?',(now_iso() if state=='archived' else None,space_id))
            from .memory import Memory
            if previous['deleted_at']:Memory.invalidate(db,space_id)
        return self.space(space_id)

    def conversations(self, space_id,include_deleted=False):
        with closing(self._connect()) as db:
            self._require(db, "research_spaces", space_id)
            return [dict(r) for r in db.execute('SELECT * FROM conversations WHERE space_id=?'+('' if include_deleted else ' AND deleted_at IS NULL')+' ORDER BY archived_at IS NOT NULL,created_at,id', (space_id,))]

    def create_conversation(self, space_id, title):
        title = text_field(title, "会话名称", 100)
        item_id, now = uuid4().hex, now_iso()
        with closing(self._connect()) as db, db:
            self._require(db, "research_spaces", space_id)
            db.execute("INSERT INTO conversations(id,space_id,title,created_at,updated_at) VALUES(?,?,?,?,?)", (item_id, space_id, title, now, now))
            db.execute('UPDATE research_spaces SET main_branch_id=coalesce(main_branch_id,?) WHERE id=?',(item_id,space_id))
            return self._require(db, "conversations", item_id, space_id)

    def history(self, space_id, conversation_id, limit=None):
        with closing(self._connect()) as db:
            self._require(db, "conversations", conversation_id, space_id)
            if limit:
                rows = db.execute("SELECT * FROM (SELECT * FROM messages WHERE conversation_id=? AND deleted_at IS NULL ORDER BY turn_seq DESC,id DESC LIMIT ?) ORDER BY turn_seq,id", (conversation_id, limit))
            else:
                rows = db.execute("SELECT * FROM messages WHERE conversation_id=? AND deleted_at IS NULL ORDER BY turn_seq,id", (conversation_id,))
            return [dict(r) for r in rows]

    @staticmethod
    def _message(db, conversation_id, role, content, intent="", job_id=None, model_id="default", model_name=""):
        now = now_iso()
        if role == 'user':
            db.execute('UPDATE conversations SET next_turn=next_turn+1 WHERE id=?',(conversation_id,))
        turn = db.execute('SELECT next_turn FROM conversations WHERE id=?',(conversation_id,)).fetchone()[0]
        parent = db.execute("SELECT id FROM messages WHERE conversation_id=? AND role='user' AND turn_seq=? ORDER BY id DESC LIMIT 1",(conversation_id,turn)).fetchone()
        parent = parent[0] if parent else None
        if job_id:
            job = db.execute('SELECT turn_seq,parent_message_id FROM research_jobs WHERE id=?',(job_id,)).fetchone()
            if job:
                turn,parent=job['turn_seq'],job['parent_message_id']
        cursor = db.execute("INSERT INTO messages(conversation_id,role,content,intent,job_id,created_at,model_id,model_name) VALUES(?,?,?,?,?,?,?,?)", (conversation_id, role, content, intent, job_id, now, model_id, model_name))
        if role == 'user':
            parent=cursor.lastrowid
        db.execute('UPDATE messages SET turn_seq=?,parent_message_id=? WHERE id=?',(turn,parent,cursor.lastrowid))
        db.execute('INSERT INTO conversation_events(conversation_id,job_id,message_id,kind,payload,created_at) VALUES(?,?,?,?,?,?)',
                   (conversation_id,job_id,cursor.lastrowid,'message','{}',now))
        db.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now, conversation_id))
        return dict(db.execute("SELECT * FROM messages WHERE id=?", (cursor.lastrowid,)).fetchone())

    def message(self, space_id, conversation_id, role, content, intent="", *, model_id="default", model_name=""):
        with closing(self._connect()) as db, db:
            self._require(db, "conversations", conversation_id, space_id)
            return self._message(db, conversation_id, role, content, intent, model_id=model_id, model_name=model_name)

    def _enqueue(self, db, space_id, conversation_id, question, brief, assumptions, retry_of=None, search_scope="auto", research_effort="standard", model_id="default", model_name="", kind="RESEARCH", payload=None):
        model_id = validate_model_id(model_id)
        model_name = text_field(model_name, "模型名称", 200, empty=True)
        if not isinstance(search_scope, str) or search_scope not in {"auto", "web", "arxiv"}:
            raise ValueError("检索范围只能是 auto、web 或 arxiv")
        if not isinstance(research_effort, str) or research_effort not in RESEARCH_BUDGETS:
            raise ValueError("研究预算不合法")
        self._require(db, "conversations", conversation_id, space_id)
        if db.execute("SELECT count(*) FROM research_jobs WHERE status IN ('queued','running')").fetchone()[0] >= 16:
            raise Conflict("后台队列已满，请等待任务完成后再试")
        item_id, now = uuid4().hex, now_iso()
        db.execute("""INSERT INTO research_jobs(id,space_id,conversation_id,question,brief,assumptions,status,stage,retry_of,created_at,updated_at,search_scope,research_effort,model_id,model_name)
                      VALUES(?,?,?,?,?,?,'queued','queued',?,?,?,?,?,?,?)""", (item_id, space_id, conversation_id, question, brief, json.dumps(assumptions, ensure_ascii=False), retry_of, now, now, search_scope, research_effort, model_id, model_name))
        if retry_of:
            parent=db.execute('SELECT parent_message_id id,turn_seq FROM research_jobs WHERE id=? AND conversation_id=?',(retry_of,conversation_id)).fetchone()
        else:
            parent=db.execute("SELECT id,turn_seq FROM messages WHERE conversation_id=? AND role='user' AND (? IS NULL OR id=?) ORDER BY turn_seq DESC,id DESC LIMIT 1",(conversation_id,(payload or {}).get('parent_message_id'),(payload or {}).get('parent_message_id'))).fetchone()
        if parent:
            db.execute('UPDATE research_jobs SET turn_seq=?,parent_message_id=? WHERE id=?',(parent['turn_seq'],parent['id'],item_id))
        reply = "我先查一下相关资料，核实后直接在这里回复你。" if research_effort == "quick" else "已创建后台研究任务。研究范围：" + brief + ("\n默认假设：" + "；".join(assumptions) if assumptions else "")
        if kind not in {'RESEARCH','MATERIAL','LOCAL_QA','DOWNLOAD','BRAINSTORM','EXPERIMENT','AUTO_RESEARCH'}:
            raise ValueError('不支持的后台任务类型')
        if kind != 'RESEARCH':
            reply = '正在检索已保存的资料与研究记录，完成后会标明依据来源。' if kind=='LOCAL_QA' else '资料任务已排队；下载、解析与分析进度会显示在右侧。'
        if kind == 'EXPERIMENT':
            reply = '受控实验已排队；将运行固定基线、调用 Codex 修改候选，再保存实际测量与比较。'
        if kind == 'AUTO_RESEARCH':
            reply = '自动研究已排队。研究模型将按目标选择调研、方案设计或委派Codex，再依据实际结果判断下一步；可随时停止并保留成果。'
        if (payload or {}).get('pending_route'):
            reply = '消息已收到，正在排队处理；进度和回答草稿会实时显示在对话区。'
        db.execute('UPDATE research_jobs SET kind=?,payload=? WHERE id=?',(kind,json.dumps(payload or {},ensure_ascii=False),item_id))
        self._message(db, conversation_id, "assistant", reply, "PENDING", item_id, model_id, model_name)
        return self._require(db, "research_jobs", item_id, space_id)

    def enqueue(self, space_id, conversation_id, question, brief, assumptions, search_scope="auto", research_effort="standard", *, model_id="default", model_name="", kind="RESEARCH", payload=None):
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            return self._enqueue(db, space_id, conversation_id, question, brief, assumptions, search_scope=search_scope, research_effort=research_effort, model_id=model_id, model_name=model_name,kind=kind,payload=payload)

    def jobs(self, space_id):
        with closing(self._connect()) as db:
            self._require(db, "research_spaces", space_id)
            return [dict(r) for r in db.execute("SELECT * FROM research_jobs WHERE space_id=? ORDER BY created_at DESC,id DESC", (space_id,)) if self.job_visible(db,r)]

    def job(self, space_id, job_id):
        with closing(self._connect()) as db:
            item = self._require(db, "research_jobs", job_id, space_id)
            report = db.execute("SELECT * FROM reports WHERE job_id=?", (job_id,)).fetchone()
            item["report"] = dict(report) if report else None
            return item

    def claim_next(self, *, parallel_only=False):
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("""SELECT j.* FROM research_jobs j
                WHERE j.status='queued' AND j.stage!='auto_waiting'
                AND (?=0 OR json_type(j.payload,'$.auto_batch')='text')
                AND NOT EXISTS(SELECT 1 FROM research_jobs r
                    WHERE r.conversation_id=j.conversation_id AND r.status IN ('running','queued')
                    AND ((r.status='running' AND NOT coalesce(
                        json_extract(j.payload,'$.auto_parent')=json_extract(r.payload,'$.auto_parent')
                        AND json_extract(j.payload,'$.auto_batch')=json_extract(r.payload,'$.auto_batch'),0))
                        OR (r.turn_seq<j.turn_seq AND r.created_at<=j.created_at
                            AND r.id!=coalesce(json_extract(j.payload,'$.auto_parent'),''))))
                ORDER BY j.created_at,j.turn_seq,j.id LIMIT 1""", (int(parallel_only),)).fetchone()
            if row is None:
                return None
            db.execute("UPDATE research_jobs SET status='running',stage='planning',updated_at=? WHERE id=?", (now_iso(), row["id"]))
            return dict(row)

    def progress(self, job_id, stage, action, trace_path):
        with closing(self._connect()) as db, db:
            changed = db.execute("UPDATE research_jobs SET stage=?,recent_action=?,trace_path=?,updated_at=? WHERE id=? AND status='running'", (stage, action, trace_path, now_iso(), job_id)).rowcount
            return bool(changed)

    def cancel(self, space_id, job_id):
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            job = self._require(db, "research_jobs", job_id, space_id)
            if job["status"] not in {"queued", "running"}:
                raise Conflict("只能取消排队或运行中的任务")
            db.execute("UPDATE research_jobs SET status='cancelled',stage='cancelled',updated_at=? WHERE id=?", (now_iso(), job_id))
            # Both workers can be busy: cancellation cannot wait for the controller tick.
            db.execute("""UPDATE research_jobs SET status='cancelled',stage='cancelled',
                error='上级研究已停止',updated_at=? WHERE space_id=? AND status IN ('queued','running')
                AND json_extract(payload,'$.auto_parent')=?""", (now_iso(), space_id, job_id))
            self._message(db, job["conversation_id"], "assistant", "任务已取消；正在进行的网络请求返回后停止，不再启动后续动作。", "RESEARCH", job_id, job["model_id"], job["model_name"])
        return self.job(space_id, job_id)

    def retry(self, space_id, job_id):
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            job = self._require(db, "research_jobs", job_id, space_id)
            if job["status"] not in {"failed", "cancelled", "interrupted"}:
                raise Conflict("只有失败、取消或中断的任务可以重试")
            if db.execute("SELECT 1 FROM research_jobs WHERE retry_of=? AND status IN ('queued','running')", (job_id,)).fetchone():
                raise Conflict("该任务已在重试中")
            return self._enqueue(db, space_id, job["conversation_id"], job["question"], job["brief"], json.loads(job["assumptions"]), job_id, job["search_scope"], job["research_effort"], job["model_id"], job["model_name"],job['kind'],json.loads(job['payload']))

    def finish(self, job_id, status, summary, error="", run_id=None, report=None):
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            job = db.execute("SELECT * FROM research_jobs WHERE id=? AND status='running'", (job_id,)).fetchone()
            if job is None:
                return False  # Cancellation won the race; never overwrite its terminal state.
            now = now_iso()
            db.execute("UPDATE research_jobs SET status=?,stage=?,summary=?,error=?,run_id=?,updated_at=? WHERE id=?", (status, status, summary, error, run_id, now, job_id))
            if report:
                db.execute("INSERT OR IGNORE INTO reports VALUES(?,?,?,?,?,?,?,?)", (job_id, job_id, job["space_id"], run_id, job["question"][:100], report[0], report[1], now))
            self._message(db, job["conversation_id"], "assistant", summary or error, job['kind'], job_id, job["model_id"], job["model_name"])
            return True

    def interrupt_pending(self):
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            jobs = db.execute("SELECT * FROM research_jobs WHERE status IN ('queued','running')").fetchall()
            for job in jobs:
                self._message(db, job["conversation_id"], "assistant", "服务重启或关闭，任务已中断。可以重试；不会自动重复外部请求。", "RESEARCH", job["id"], job["model_id"], job["model_name"])
            db.execute("UPDATE research_jobs SET status='interrupted',stage='interrupted',error='服务中断，请重试',updated_at=? WHERE status IN ('queued','running')", (now_iso(),))
            return len(jobs)
