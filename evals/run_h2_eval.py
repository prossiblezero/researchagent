"""Deterministic H2 safety and recovery behavior gate."""

from __future__ import annotations

import argparse
import hashlib
import json
import socket
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import research_agent.search as search_module
from research_agent import (
    ModelDecision,
    OpenAICompatibleModel,
    ReadResponse,
    ResearchAgent,
    RunStore,
    SearchResponse,
)
from research_agent.policy import before_finalize, before_tool


PUBLIC_ADDRESSES = [
    (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 443)),
]
PROBE_DATA = {
    "private_urls": [
        "http://127.0.0.1/private",
        "http://localhost/private",
        "http://10.0.0.1/private",
        "http://169.254.169.254/private",
        "http://[::1]/private",
        "http://2130706433/private",
    ],
    "credential_cases": [
        {
            "id": "userinfo",
            "url": "https://user:fake-password@example.com/private",
            "secret": "fake-password",
        },
        {
            "id": "access-token",
            "url": "https://example.com/private?access_token=fake-access-secret",
            "secret": "fake-access-secret",
        },
        {
            "id": "encoded-key",
            "url": "https://example.com/private?access%5Ftoken=fake-encoded-secret",
            "secret": "fake-encoded-secret",
        },
    ],
    "source_url": "https://example.com/source",
    "media_types": ["text/html; charset=utf-8", "text/plain", "application/pdf", "application/json", ""],
}
CODE_FILES = tuple(
    ROOT / path
    for path in (
        "research_agent/contracts.py",
        "research_agent/search.py",
        "research_agent/policy.py",
        "research_agent/models.py",
        "research_agent/loop.py",
        "research_agent/evidence.py",
        "research_agent/trace.py",
        "research_agent/storage.py",
    )
)
EVALUATION_FILES = (Path(__file__).resolve(), ROOT / "tests/test_h2.py")


class SequenceModel:
    name = "h2-sequence-model"

    def __init__(self, *decisions: ModelDecision):
        self.decisions = list(decisions)
        self.tools_seen: list[list[dict[str, Any]]] = []

    def complete(self, messages, tools):
        self.tools_seen.append(tools)
        if not self.decisions:
            raise AssertionError("unexpected model call")
        return self.decisions.pop(0)


class ScriptedSearch:
    def __init__(self, *responses: SearchResponse):
        self.responses = list(responses)
        self.calls = 0

    def search(self, query: str) -> SearchResponse:
        self.calls += 1
        if not self.responses:
            raise AssertionError("unexpected search call")
        return self.responses.pop(0)


class ScriptedReader:
    def __init__(self, response: ReadResponse):
        self.response = response
        self.calls = 0

    def read(self, url: str) -> ReadResponse:
        self.calls += 1
        return self.response


class FakeResponse:
    def __init__(self, payload: dict[str, Any]):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self) -> bytes:
        return json.dumps(self.payload).encode()


def source_response(query: str = "topic", network_requests: int = 1) -> SearchResponse:
    return SearchResponse(
        True,
        query,
        [{"title": "Source", "url": PROBE_DATA["source_url"], "snippet": "fact"}],
        network_requests=network_requests,
    )


def check(name: str, actual: Any, expected: Any) -> dict[str, Any]:
    return {"name": name, "expected": expected, "actual": actual, "passed": actual == expected}


def row(probe_id: str, assertions: list[dict[str, Any]]) -> dict[str, Any]:
    return {"id": probe_id, "passed": all(item["passed"] for item in assertions), "assertions": assertions}


def credential_probe() -> dict[str, Any]:
    assertions = []
    for case in PROBE_DATA["credential_cases"]:
        label, url, secret = case["id"], case["url"], case["secret"]
        audit = before_tool("read", url=url, allowed_urls={url})
        assertions.extend((
            check(f"{label}.decision", audit.decision, "deny"),
            check(f"{label}.reason", audit.reason, "credential_url"),
            check(f"{label}.secret_in_target", secret in audit.target, False),
        ))

        model = SequenceModel(ModelDecision("tool_call", tool_name="read", url=url, call_id=f"{label}-read"))
        reader = ScriptedReader(ReadResponse(True, url, "must not run", network_requests=1))
        with tempfile.TemporaryDirectory() as tmp:
            result = ResearchAgent(ScriptedSearch(), model, tmp, reader=reader).run("question?")
            trace = Path(result.trace_path).read_text(encoding="utf-8")
            database = Path(tmp) / "runs.db"
            store = RunStore(database)
            run_id = store.save(result, "question?", "2026-09-14T00:00:00+00:00")
            stored = json.dumps(store.get(run_id), ensure_ascii=False)
            database_bytes = database.read_bytes()
        assertions.extend((
            check(f"{label}.termination", result.termination, "policy_denied"),
            check(f"{label}.reader_calls", reader.calls, 0),
            check(f"{label}.secret_in_trace", secret in trace, False),
            check(f"{label}.secret_in_result", any(secret in event.target for event in result.audit_events), False),
            check(f"{label}.secret_in_store", secret in stored or secret.encode() in database_bytes, False),
        ))
    return row("credential_url_blocked", assertions)


def private_targets_probe() -> dict[str, Any]:
    assertions = []
    with (
        patch.object(search_module, "resolve_public_addresses") as resolve,
        patch.object(search_module, "_request_once") as request,
    ):
        for index, url in enumerate(PROBE_DATA["private_urls"], 1):
            audit = before_tool("read", url=url, allowed_urls={url})
            response = search_module.HttpReader().read(url)
            assertions.extend((
                check(f"url_{index}.policy", [audit.decision, audit.reason], ["deny", "network_target_denied"]),
                check(f"url_{index}.reader_ok", response.ok, False),
                check(f"url_{index}.reader_error", (response.error or {}).get("code"), "network_target_denied"),
                check(f"url_{index}.network_requests", response.network_requests, 0),
            ))
    assertions.extend((
        check("resolver_calls", resolve.call_count, 0),
        check("http_request_calls", request.call_count, 0),
    ))
    return row("private_targets", assertions)


def dns_probe() -> dict[str, Any]:
    mixed_addresses = [
        *PUBLIC_ADDRESSES,
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("10.0.0.1", 443)),
    ]
    direct_error = ""
    try:
        search_module.resolve_public_addresses("example.com", 443, resolver=Mock(return_value=mixed_addresses))
    except Exception as exc:  # captured as an observable result in the report
        direct_error = type(exc).__name__
    with (
        patch.object(
            search_module,
            "resolve_public_addresses",
            side_effect=search_module.UnsafeNetworkTarget("mixed public/private DNS answers"),
        ) as resolve,
        patch.object(search_module, "_request_once") as request,
    ):
        response = search_module.HttpReader().read(PROBE_DATA["source_url"])
    return row("http_reader_dns", [
        check("direct_resolver_exception", direct_error, "UnsafeNetworkTarget"),
        check("reader_ok", response.ok, False),
        check("reader_error", (response.error or {}).get("code"), "network_target_denied"),
        check("network_requests", response.network_requests, 0),
        check("resolver_calls", resolve.call_count, 1),
        check("http_request_calls", request.call_count, 0),
    ])


def redirect_probe() -> dict[str, Any]:
    with (
        patch.object(search_module, "resolve_public_addresses", return_value=PUBLIC_ADDRESSES) as private_resolve,
        patch.object(
            search_module,
            "_request_once",
            return_value=search_module._NetworkResponse(302, {"location": "http://127.0.0.1/private"}, b""),
        ) as private_request,
    ):
        private = search_module.HttpReader().read(PROBE_DATA["source_url"])

    with (
        patch.object(
            search_module,
            "resolve_public_addresses",
            side_effect=[PUBLIC_ADDRESSES, search_module.UnsafeNetworkTarget("private redirect DNS")],
        ) as dns_resolve,
        patch.object(
            search_module,
            "_request_once",
            return_value=search_module._NetworkResponse(
                302,
                {"location": "https://redirect.example/private"},
                b"",
            ),
        ) as dns_request,
    ):
        dns = search_module.HttpReader().read(PROBE_DATA["source_url"])
    return row("http_reader_redirect", [
        check("private_target.error", (private.error or {}).get("code"), "unsafe_redirect"),
        check("private_target.network_requests", private.network_requests, 1),
        check("private_target.resolver_calls", private_resolve.call_count, 1),
        check("private_target.http_request_calls", private_request.call_count, 1),
        check("private_dns.error", (dns.error or {}).get("code"), "network_target_denied"),
        check("private_dns.network_requests", dns.network_requests, 1),
        check("private_dns.resolver_calls", dns_resolve.call_count, 2),
        check("private_dns.http_request_calls", dns_request.call_count, 1),
    ])


def media_type_probe() -> dict[str, Any]:
    assertions = []
    bodies = {
        "text/html; charset=utf-8": b"<p>html fact</p>",
        "text/plain": b"plain fact",
        "application/pdf": b"%PDF fake",
        "application/json": b"{}",
        "": b"missing type",
    }
    for index, content_type in enumerate(PROBE_DATA["media_types"], 1):
        headers = {"content-type": content_type} if content_type else {}
        with (
            patch.object(search_module, "resolve_public_addresses", return_value=PUBLIC_ADDRESSES),
            patch.object(
                search_module,
                "_request_once",
                return_value=search_module._NetworkResponse(200, headers, bodies[content_type]),
            ) as request,
        ):
            response = search_module.HttpReader().read(PROBE_DATA["source_url"])
        supported = content_type.startswith("text/")
        assertions.extend((
            check(f"type_{index}.ok", response.ok, supported),
            check(
                f"type_{index}.error",
                (response.error or {}).get("code", ""),
                "" if supported else "unsupported_media_type",
            ),
            check(f"type_{index}.has_content", bool(response.content), supported),
            check(f"type_{index}.network_requests", response.network_requests, 1),
            check(f"type_{index}.http_request_calls", request.call_count, 1),
        ))
    return row("http_reader_media_types", assertions)


def parameter_recovery_probe() -> dict[str, Any]:
    model = SequenceModel(
        ModelDecision("tool_call", query="", call_id="bad-1"),
        ModelDecision("tool_call", query="x" * 301, call_id="bad-2"),
        ModelDecision("tool_call", query="topic", call_id="fixed"),
        ModelDecision("final", "fact [S1]"),
    )
    search = ScriptedSearch(source_response())
    with tempfile.TemporaryDirectory() as tmp:
        result = ResearchAgent(search, model, tmp, max_tool_calls=1, max_rounds=4).run("question?")
    return row("parameter_recovery", [
        check("model_calls", len(model.tools_seen), 4),
        check("search_calls", search.calls, 1),
        check("status", result.status, "ok"),
        check("termination", result.termination, "model_final"),
        check("counters", [result.tool_attempts, result.network_requests, result.tool_denials, result.tool_successes], [3, 1, 0, 1]),
    ])


def malformed_arguments_probe() -> dict[str, Any]:
    responses = [
        FakeResponse({
            "choices": [{
                "message": {"tool_calls": [{"id": "bad", "function": {"name": "search", "arguments": "{"}}]},
                "finish_reason": "tool_calls",
            }],
        }),
        FakeResponse({
            "choices": [{
                "message": {"tool_calls": [{
                    "id": "fixed",
                    "function": {"name": "search", "arguments": json.dumps({"query": "topic"})},
                }]},
                "finish_reason": "tool_calls",
            }],
        }),
        FakeResponse({"choices": [{"message": {"content": "fact [S1]"}, "finish_reason": "stop"}]}),
    ]
    model = OpenAICompatibleModel("https://api.example/v1", "fake-api-key", "model")
    search = ScriptedSearch(source_response())
    with tempfile.TemporaryDirectory() as tmp, patch("research_agent.models.urlopen", side_effect=responses) as urlopen:
        result = ResearchAgent(search, model, tmp, max_tool_calls=1).run("question?")
    return row("malformed_tool_arguments", [
        check("model_http_calls", urlopen.call_count, 3),
        check("search_calls", search.calls, 1),
        check("status", result.status, "ok"),
        check("termination", result.termination, "model_final"),
        check("counters", [result.tool_attempts, result.network_requests, result.tool_denials, result.tool_successes], [2, 1, 0, 1]),
    ])


def retry_probe() -> dict[str, Any]:
    transient = ScriptedSearch(
        SearchResponse(False, "topic", error={"code": "search_transient", "message": "timeout"}, network_requests=1),
        source_response(),
    )
    transient_model = SequenceModel(
        ModelDecision("tool_call", query="topic", call_id="search"),
        ModelDecision("final", "fact [S1]"),
    )
    with tempfile.TemporaryDirectory() as tmp:
        transient_result = ResearchAgent(transient, transient_model, tmp, max_tool_calls=1).run("question?")

    permanent = ScriptedSearch(SearchResponse(
        False,
        "topic",
        error={"code": "invalid_request", "message": "bad request"},
        network_requests=1,
    ))
    permanent_model = SequenceModel(
        ModelDecision("tool_call", query="topic", call_id="search"),
        ModelDecision("final", "INSUFFICIENT: invalid request"),
    )
    with tempfile.TemporaryDirectory() as tmp:
        permanent_result = ResearchAgent(permanent, permanent_model, tmp, max_tool_calls=1).run("question?")
    return row("network_retry", [
        check("transient.provider_calls", transient.calls, 2),
        check("transient.status", transient_result.status, "ok"),
        check("transient.counters", [transient_result.tool_attempts, transient_result.network_requests, transient_result.tool_denials, transient_result.tool_successes], [1, 2, 0, 1]),
        check("non_transient.provider_calls", permanent.calls, 1),
        check("non_transient.status", permanent_result.status, "insufficient"),
        check("non_transient.counters", [permanent_result.tool_attempts, permanent_result.network_requests, permanent_result.tool_denials, permanent_result.tool_successes], [1, 1, 0, 0]),
    ])


def budget_probe() -> dict[str, Any]:
    model = SequenceModel(
        ModelDecision("tool_call", query="topic", call_id="search"),
        ModelDecision("final", "fact [S1]"),
    )
    search = ScriptedSearch(source_response())
    with tempfile.TemporaryDirectory() as tmp:
        result = ResearchAgent(search, model, tmp, max_tool_calls=1).run("question?")
    return row("budget_final_only", [
        check("model_calls", len(model.tools_seen), 2),
        check("first_call_has_tools", bool(model.tools_seen[0]), True),
        check("final_call_tools", model.tools_seen[1], []),
        check("search_calls", search.calls, 1),
        check("status", result.status, "ok"),
        check("termination", result.termination, "model_final"),
        check("counters", [result.tool_attempts, result.network_requests, result.tool_denials, result.tool_successes], [1, 1, 0, 1]),
    ])


def final_only_denial_probe() -> dict[str, Any]:
    model = SequenceModel(
        ModelDecision("tool_call", query="topic", call_id="search"),
        ModelDecision("tool_call", query="must-not-run", call_id="forbidden"),
    )
    search = ScriptedSearch(source_response())
    with tempfile.TemporaryDirectory() as tmp:
        result = ResearchAgent(search, model, tmp, max_tool_calls=1).run("question?")
    return row("final_only_rejection_counts", [
        check("final_call_tools", model.tools_seen[-1], []),
        check("search_calls", search.calls, 1),
        check("termination", result.termination, "budget_exhausted"),
        check("counters", [result.tool_attempts, result.network_requests, result.tool_denials, result.tool_successes], [2, 1, 1, 1]),
    ])


def network_denial_count_probe() -> dict[str, Any]:
    model = SequenceModel(
        ModelDecision("tool_call", query="topic", call_id="search"),
        ModelDecision("tool_call", tool_name="read", url=PROBE_DATA["source_url"], call_id="read"),
        ModelDecision("final", "INSUFFICIENT: unsafe network target"),
    )
    search = ScriptedSearch(source_response())
    with (
        tempfile.TemporaryDirectory() as tmp,
        patch.object(
            search_module,
            "resolve_public_addresses",
            side_effect=search_module.UnsafeNetworkTarget("private DNS answer"),
        ) as resolve,
        patch.object(search_module, "_request_once") as request,
    ):
        result = ResearchAgent(
            search,
            model,
            tmp,
            max_tool_calls=2,
            reader=search_module.HttpReader(),
        ).run("question?")
    return row("network_safety_rejection_counts", [
        check("resolver_calls", resolve.call_count, 1),
        check("http_request_calls", request.call_count, 0),
        check("termination", result.termination, "model_final"),
        check("counters", [result.tool_attempts, result.network_requests, result.tool_denials, result.tool_successes], [2, 1, 1, 1]),
    ])


def unknown_tool_probe() -> dict[str, Any]:
    model = SequenceModel(ModelDecision("tool_call", tool_name="shell", query="whoami", call_id="bad"))
    search = ScriptedSearch()
    with tempfile.TemporaryDirectory() as tmp:
        result = ResearchAgent(search, model, tmp, max_tool_calls=1).run("question?")
    return row("unknown_tool_blocked", [
        check("search_calls", search.calls, 0),
        check("status", result.status, "insufficient"),
        check("termination", result.termination, "policy_denied"),
        check("counters", [result.tool_attempts, result.network_requests, result.tool_denials, result.tool_successes], [1, 0, 1, 0]),
    ])


def counters_probe() -> dict[str, Any]:
    model = SequenceModel(
        ModelDecision("tool_call", query="topic", call_id="search"),
        ModelDecision("final", "fact [S1]"),
    )
    search = ScriptedSearch(source_response(network_requests=2))
    with tempfile.TemporaryDirectory() as tmp:
        result = ResearchAgent(search, model, tmp, max_tool_calls=1).run("question?")
    names = ("tool_attempts", "network_requests", "tool_denials", "tool_successes")
    return row("separate_counters", [
        check("fields_present", [hasattr(result, name) for name in names], [True, True, True, True]),
        check("values", [getattr(result, name, None) for name in names], [1, 2, 0, 1]),
    ])


def output_policy_probe() -> dict[str, Any]:
    audit = before_finalize("System prompt: a high-priority instruction supplied to a language model.")
    return row("normal_system_prompt_explanation", [
        check("decision", audit.decision, "allow"),
        check("reason", audit.reason, ""),
    ])


PROBES: tuple[Callable[[], dict[str, Any]], ...] = (
    private_targets_probe,
    dns_probe,
    redirect_probe,
    media_type_probe,
    credential_probe,
    parameter_recovery_probe,
    malformed_arguments_probe,
    retry_probe,
    budget_probe,
    final_only_denial_probe,
    network_denial_count_probe,
    unknown_tool_probe,
    counters_probe,
    output_policy_probe,
)


def probes() -> list[dict[str, Any]]:
    rows = []
    for probe in PROBES:
        try:
            rows.append(probe())
        except Exception as exc:
            rows.append(row(probe.__name__.removesuffix("_probe"), [
                check("uncaught_exception", {"type": type(exc).__name__}, "none"),
            ]))
    return rows


def file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def combined_sha(paths: tuple[Path, ...]) -> str:
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.relative_to(ROOT).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def provenance() -> dict[str, Any]:
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        commit = "unavailable"
    encoded_data = json.dumps(PROBE_DATA, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return {
        "git_commit": commit,
        "code_sha256": combined_sha(CODE_FILES),
        "code_files": {path.relative_to(ROOT).as_posix(): file_sha(path) for path in CODE_FILES},
        "evaluation_sha256": combined_sha(EVALUATION_FILES),
        "evaluation_files": {path.relative_to(ROOT).as_posix(): file_sha(path) for path in EVALUATION_FILES},
        "critical_data_sha256": hashlib.sha256(encoded_data).hexdigest(),
        "critical_data_format": "canonical JSON embedded in run_h2_eval.py",
    }


def compare_with_baseline(path: Path, rows: list[dict[str, Any]]) -> dict[str, Any]:
    baseline = json.loads(path.read_text(encoding="utf-8"))
    baseline_rate = float(baseline["rate"])
    current_by_id = {item["id"]: item for item in rows}
    baseline_rows = baseline.get("rows", [])
    baseline_ids = [item["id"] for item in baseline_rows]
    missing = [probe_id for probe_id in baseline_ids if probe_id not in current_by_id]
    current_passed = sum(bool(current_by_id.get(probe_id, {}).get("passed")) for probe_id in baseline_ids)
    current_rate = current_passed / len(baseline_ids) if baseline_ids else 0.0
    prior_passes = [item["id"] for item in baseline_rows if item.get("passed")]
    regressions = [probe_id for probe_id in prior_passes if not current_by_id.get(probe_id, {}).get("passed", False)]
    return {
        "path": str(path),
        "sha256": file_sha(path),
        "benchmark": baseline.get("benchmark"),
        "comparison_basis": "shared acceptance IDs; current probes are equal or stricter",
        "passed": baseline.get("passed"),
        "total": baseline.get("total"),
        "rate": baseline_rate,
        "current_passed": current_passed,
        "current_total": len(baseline_ids),
        "current_rate": current_rate,
        "strict_improvement": current_rate > baseline_rate,
        "missing_probe_ids": missing,
        "previously_passing_regressions": regressions,
        "passed_gate": current_rate > baseline_rate and not missing and not regressions,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run deterministic H2 safety/recovery behavior probes")
    parser.add_argument("--label", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--baseline", type=Path)
    args = parser.parse_args()
    if args.baseline and args.output.resolve() == args.baseline.resolve():
        parser.error("--output must not overwrite --baseline")

    rows = probes()
    passed = sum(item["passed"] for item in rows)
    rate = passed / len(rows)
    comparison = compare_with_baseline(args.baseline, rows) if args.baseline else None
    gate_passed = passed == len(rows) and (comparison is None or comparison["passed_gate"])
    report = {
        "benchmark": "h2-safety-recovery-v2",
        "label": args.label,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "provenance": provenance(),
        "passed": passed,
        "total": len(rows),
        "rate": rate,
        "gate_passed": gate_passed,
        "comparison": comparison,
        "rows": rows,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if gate_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
