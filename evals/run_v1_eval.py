"""Reproducible V1 workflow smoke; --live uses configured services, never fixture fallback."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from main import build_search
from research_agent import FailingSearch, FixtureReader, HttpReader, OfflineModel, ResearchAgent, load_dotenv, model_from_env
from research_agent.trace import now_iso, redact
from research_agent.workbench import OfflineRouter, Workbench
from research_agent.workbench_store import RESEARCH_BUDGETS, WorkbenchStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--question", default="我想研究一下 Agent")
    parser.add_argument("--search-scope", choices=("auto", "web", "arxiv"), default="auto")
    args = parser.parse_args()
    load_dotenv(args.env_file)
    os.environ["OFFLINE_MODE"] = "0" if args.live else "1"
    mode = "live" if args.live else "offline"
    tag = "v1-" + mode + "-" + uuid4().hex[:10]
    folder = ROOT / "data" / "v1-eval" / tag
    output = args.output or ROOT / "evals" / "reports" / (tag + ".json")
    if output.exists():
        parser.error("Output exists; choose a new path to preserve previous evidence")
    store = WorkbenchStore(folder / "workbench.db")
    rows = []

    def model():
        client = model_from_env()
        if args.live and isinstance(client, OfflineModel):
            raise ValueError("Live model credentials are missing")
        if hasattr(client, "timeout"):
            client.timeout = 45
        return client

    def agent(observer):
        if args.live and not any(os.getenv(k) for k in ("TAVILY_API_KEY", "SEARCH_API_KEY", "SEARCH_URL")):
            raise ValueError("Live search credentials are missing")
        return ResearchAgent(build_search(), model(), folder / "traces", reader=HttpReader() if args.live else FixtureReader.from_file(ROOT / "fixtures" / "search_results.json"), max_tool_calls=16, max_rounds=20, on_event=observer)

    app = Workbench(store, model if args.live else OfflineRouter, agent, folder / "traces", start_worker=False)
    try:
        a = store.save_space({"name": "V1 acceptance"})["id"]
        b = store.save_space({"name": "Isolated space"})["id"]
        ca = store.create_conversation(a, "Smoke")["id"]
        cb = store.create_conversation(b, "Untouched")["id"]
        reply = app.send(a, ca, "你好")
        rows.append({"case": "greeting_without_research", "passed": reply["intent"] == "CHAT" and not store.jobs(a), "intent": reply["intent"]})
        reply = app.send(a, ca, args.question, args.search_scope)
        rows.append({"case": "research_queued", "passed": reply["intent"] == "RESEARCH" and bool(reply.get("task_id")), "intent": reply["intent"]})
        if reply.get("task_id"):
            app.execute(store.claim_next())
            job = store.job(a, reply["task_id"])
            run = store.get(job["run_id"]) if job["run_id"] else None
            rows.append({"case": "research_report", "passed": job["status"] == "completed" and bool(job["report"]), "job": job, "run": run})
        rows.append({"case": "space_isolation", "passed": store.history(b, cb) == [] and store.jobs(b) == []})
        if not args.live:
            app.agent_factory = lambda observer: ResearchAgent(FailingSearch(), OfflineModel(), folder / "traces", on_event=observer)
            failure = app.send(a, ca, "请核验失败来源")["task_id"]
            app.execute(store.claim_next())
            job = store.job(a, failure)
            rows.append({"case": "failure_is_retryable", "passed": job["status"] == "failed" and "INSUFFICIENT" in job["summary"], "job": job})
            queued = app.retry(a, failure)
            app.close()
            rows.append({"case": "interrupted_after_shutdown", "passed": store.job(a, queued["id"])["status"] == "interrupted", "task_id": queued["id"]})
    except Exception as exc:
        rows.append({"case": "workflow_error", "passed": False, "error": str(redact(str(exc)))})
    finally:
        app.close()
    hashes = {str(p.relative_to(ROOT)).replace("\\", "/"): hashlib.sha256(p.read_bytes()).hexdigest() for p in [ROOT / "server.py", *sorted(p for p in (ROOT / "web").rglob("*") if p.is_file()), *sorted((ROOT / "research_agent").glob("*.py")), ROOT / "tests" / "test_workbench.py", ROOT / "tests" / "test_workbench_ui.py", Path(__file__)]}
    report = {"suite": "researchagent-v1-workflow", "created_at": now_iso(), "mode": mode, "quality_judgment": "UNJUDGED", "scope": "Workflow integration, not research correctness or general model quality", "budget": {"policy": "model-selected effort; early completion allowed", "tool_limits": RESEARCH_BUDGETS, "extra_rounds": 4, "model_timeout_s": 45}, "passed": sum(r["passed"] for r in rows), "total": len(rows), "source_sha256": hashes, "artifact_root": str(folder.relative_to(ROOT)), "rows": rows}
    def portable(value):
        if isinstance(value, dict):
            return {k: portable(v) for k, v in value.items()}
        if isinstance(value, list):
            return [portable(v) for v in value]
        if isinstance(value, str) and value.startswith(str(ROOT) + os.sep):
            return str(Path(value).relative_to(ROOT)).replace("\\", "/")
        return value
    output.parent.mkdir(parents=True, exist_ok=True)
    # Existing harness redaction bounds each string, preserving the full structured report.
    output.write_text(json.dumps(portable(redact(report)), ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(output), "mode": mode, "passed": report["passed"], "total": report["total"]}, ensure_ascii=False))
    return 0 if rows and all(r["passed"] for r in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
