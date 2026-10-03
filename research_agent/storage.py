"""SQLite persistence for completed research runs."""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from .contracts import RunResult


class RunStore:
    def __init__(self, path: str | Path = "data/evidence_agent.db") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _init(self) -> None:
        with closing(self._connect()) as conn, conn:
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, question TEXT NOT NULL, answer TEXT NOT NULL, status TEXT NOT NULL, termination TEXT NOT NULL, tool_calls INTEGER NOT NULL, trace_path TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS sources (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE, source_id TEXT NOT NULL, title TEXT NOT NULL, url TEXT NOT NULL, snippet TEXT NOT NULL, publisher TEXT NOT NULL, published_at TEXT, retrieved_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS evidence (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE, evidence_id TEXT NOT NULL, source_id TEXT NOT NULL, content TEXT NOT NULL, kind TEXT NOT NULL, retrieved_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS claims (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE, claim_id TEXT NOT NULL, statement TEXT NOT NULL, evidence_ids TEXT NOT NULL, status TEXT NOT NULL, confidence REAL, reason TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS experiences (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE, step INTEGER NOT NULL, action TEXT NOT NULL, observation TEXT NOT NULL, feedback TEXT NOT NULL, lesson TEXT NOT NULL, next_action TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS audit_events (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE, hook TEXT NOT NULL, decision TEXT NOT NULL, reason TEXT NOT NULL, target TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS idx_runs_created_at ON runs(created_at DESC);
            """)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(evidence)")}
            for name, definition in (
                ("summary", "TEXT NOT NULL DEFAULT ''"),
                ("title", "TEXT NOT NULL DEFAULT ''"),
                ("content_hash", "TEXT NOT NULL DEFAULT ''"),
                ("truncated", "INTEGER NOT NULL DEFAULT 0"),
                ("provenance", "TEXT NOT NULL DEFAULT '{}'"),
            ):
                if name not in columns:
                    conn.execute(f"ALTER TABLE evidence ADD COLUMN {name} {definition}")

    def save(self, result: RunResult, question: str, created_at: str) -> str:
        run_id = Path(result.trace_path).stem
        with closing(self._connect()) as conn, conn:
            conn.execute("INSERT OR REPLACE INTO runs VALUES(?,?,?,?,?,?,?,?)", (run_id, question, result.answer, result.status, result.termination, result.tool_calls, result.trace_path, created_at))
            for table in ("sources", "evidence", "claims", "experiences", "audit_events"):
                conn.execute(f"DELETE FROM {table} WHERE run_id = ?", (run_id,))
            conn.executemany("INSERT INTO sources(run_id,source_id,title,url,snippet,publisher,published_at,retrieved_at) VALUES(?,?,?,?,?,?,?,?)", [(run_id, x.source_id, x.title, x.url, x.snippet, x.publisher, x.published_at, x.retrieved_at) for x in result.sources])
            conn.executemany(
                "INSERT INTO evidence(run_id,evidence_id,source_id,content,kind,retrieved_at,summary,title,content_hash,truncated,provenance) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        run_id,
                        x.evidence_id,
                        x.source_id,
                        x.content,
                        x.kind,
                        x.retrieved_at,
                        x.summary,
                        x.title,
                        x.content_hash,
                        int(x.truncated),
                        json.dumps(x.provenance,ensure_ascii=False),
                    )
                    for x in result.evidence
                ],
            )
            conn.executemany("INSERT INTO claims(run_id,claim_id,statement,evidence_ids,status,confidence,reason) VALUES(?,?,?,?,?,?,?)", [(run_id, x.claim_id, x.statement, json.dumps(x.evidence_ids, ensure_ascii=False), x.status, x.confidence, x.reason) for x in result.claims])
            conn.executemany("INSERT INTO experiences(run_id,step,action,observation,feedback,lesson,next_action) VALUES(?,?,?,?,?,?,?)", [(run_id, x.step, x.action, x.observation, x.feedback, x.lesson, x.next_action) for x in result.experiences])
            conn.executemany("INSERT INTO audit_events(run_id,hook,decision,reason,target) VALUES(?,?,?,?,?)", [(run_id, x.hook, x.decision, x.reason, x.target) for x in result.audit_events])
        return run_id

    def list_runs(self, limit: int = 30) -> list[dict[str, Any]]:
        with closing(self._connect()) as conn:
            rows = conn.execute("SELECT id,question,status,termination,tool_calls,created_at FROM runs ORDER BY created_at DESC LIMIT ?", (max(1, min(limit, 100)),)).fetchall()
        return [dict(row) for row in rows]

    def get(self, run_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as conn:
            run = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            if run is None:
                return None
            payload = dict(run)
            for table, key, columns in (("sources", "sources", "source_id,title,url,snippet,publisher,published_at,retrieved_at"), ("evidence", "evidence", "evidence_id,source_id,content,kind,retrieved_at,summary,title,content_hash,truncated,provenance"), ("claims", "claims", "claim_id,statement,evidence_ids,status,confidence,reason"), ("experiences", "experiences", "step,action,observation,feedback,lesson,next_action"), ("audit_events", "audit_events", "hook,decision,reason,target")):
                items = [dict(row) for row in conn.execute(f"SELECT {columns} FROM {table} WHERE run_id = ? ORDER BY id", (run_id,)).fetchall()]
                if table == "claims":
                    for item in items:
                        item["evidence_ids"] = json.loads(item["evidence_ids"])
                elif table == "evidence":
                    for item in items:
                        item["truncated"] = bool(item["truncated"])
                        item["provenance"] = json.loads(item["provenance"])
                payload[key] = items
            return payload
