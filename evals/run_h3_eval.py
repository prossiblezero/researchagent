"""Deterministic H3 extraction and context-budget gate (offline only)."""

from __future__ import annotations

import argparse
import copy
import hashlib
import importlib
import inspect
import json
import math
import platform
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import research_agent.search as search_module
from research_agent import (
    Claim,
    Evidence,
    HttpReader,
    ModelDecision,
    ReadResponse,
    ResearchAgent,
    RunResult,
    RunStore,
    SearchResponse,
    Source,
)
from research_agent.contracts import SYSTEM_PROMPT, TOOL_SCHEMA
from research_agent.trace import redact


SCHEMA_VERSION = "h3-eval-7"
SOURCE_URL = "https://example.com/h3-source"
CONFIG = {
    "estimator": "ceil(canonical_utf8_bytes/3); deterministic comparison only, not tokenizer output",
    "direct_context": {
        "max_context_tokens": 5000,
        "output_reserve_tokens": 700,
        "safety_margin_tokens": 300,
    },
    "loop_context": {
        "max_context_tokens": 3200,
        "output_reserve_tokens": 500,
        "safety_margin_tokens": 300,
    },
    "http_truncation_bytes": 128,
    "short_read_budget_tokens": 693,
    "historical_pre_h3_cost": {
        "report": "evals/reports/h3-before.json",
        "total_model_input_bytes": 69493,
        "total_model_input_estimated_tokens": 23166,
    },
    "required_rows": [
        "html_extraction",
        "explicit_truncation",
        "evidence_separation",
        "credential_redaction",
        "sqlite_compatible_migration",
        "structured_context",
        "short_read_budget",
        "loop_context_trace",
        "protected_over_budget",
        "loop_quality_guard",
    ],
}
HTML_FIXTURE = """<!doctype html><html><head>
<title>Structured Evidence Guide</title>
<style>.NOISE_STYLE_SENTINEL { display:none }</style>
<script>window.NOISE_SCRIPT_SENTINEL = true;</script></head><body>
<header>NOISE_HEADER_SENTINEL</header><nav>NOISE_NAV_SENTINEL</nav>
<main><header><h1>Evidence Budget Guide</h1>
<p>KEEP_ENGLISH_SENTINEL: verified context remains attributable.</p>
<p>KEEP_SEMANTIC_HEADER_SENTINEL: article metadata is evidence.</p>
<nav><header>NOISE_NESTED_HEADER_SENTINEL</header></nav></header>
<p>保留中文正文：上下文预算不能破坏证据关系。</p>
<div>KEEP_DIV_SENTINEL: plain div evidence remains visible.</div>
<p>KEEP_DUPLICATE_SENTINEL</p><p>KEEP_DUPLICATE_SENTINEL</p>
<table><tr><th>KEEP_TABLE_HEADER</th><th>Value</th></tr>
<tr><td>KEEP_TABLE_METRIC</td><td>97%</td></tr></table>
<ul><li>KEEP_LIST_SENTINEL uses source S1 and evidence E1.
<ul><li>KEEP_NESTED_SENTINEL</li></ul>KEEP_NESTED_TAIL</li></ul>
<pre><code>def keep_pair(value):
    return value if value &lt; 3 else "[S1]-[E1]"
</code></pre>
<p>KEEP_TAIL_SENTINEL: body extraction reached the final paragraph.</p></main>
<aside>NOISE_ASIDE_SENTINEL</aside><footer>NOISE_FOOTER_SENTINEL</footer>
</body></html>"""
PROBE_DATA = {
    "html_sha256": hashlib.sha256(HTML_FIXTURE.encode("utf-8")).hexdigest(),
    "source_url": SOURCE_URL,
    "quality_fact_values": ["ORBIT_17", "QUARTZ_29", "VISIBLE_7319", "REGRESSION_A", "REGRESSION_B"],
    "credential_field_cases": [
        "GITHUBTOKEN",
        "DATABASEPASSWORD",
        "_access_token",
        "-api_key",
        "escaped accessToken",
        "escaped inner quote",
        "nested sensitive assignments",
        "unquoted delimiter suffixes",
        "redacted placeholder with credential suffix",
        "redacted placeholder with unquoted delimiter suffixes",
        "redacted placeholder followed by quoted credential assignment",
        "129-char prefix + _token",
        "ordinary key text preserved",
    ],
    "credential_json_cases": [
        {"message": "password=[REDACTED]", "prompt_tokens": 12},
        {"message": "password=[REDACTED]", "note": "ordinary text"},
        {"message": "password=[REDACTED]JSON_TAIL_SECRET", "prompt_tokens": 12, "note": "ordinary text"},
        {"sk-synthetic_key_927461": "ordinary text", "prompt_tokens": 12},
        {"nested": [{"sk-synthetic_key_927461": 12}], "note": "ordinary text"},
    ],
    "credential_json_wrappers": [["", ""], ["Result: ", " done"], ["```json\n", "\n```"],
                                 ["说明：\n```json\n", "\n```\n结束"]],
    "credential_json_encodings": ["plain", "string", "escaped"],
    "context_markers": [
        "中文上下文必须保留",
        "def 保留(value): return '[S1]-[E1]'",
        "C-open",
        "C-conflict",
        "latest-error-sentinel",
        "call-3",
        "call-4",
    ],
    "short_read_budget": {"system_padding_chars": 1415, "body_chars": [3000, 25, 25]},
}
CODE_FILES = tuple(
    ROOT / path
    for path in (
        "research_agent/contracts.py",
        "research_agent/search.py",
        "research_agent/evidence.py",
        "research_agent/context.py",
        "research_agent/loop.py",
        "research_agent/models.py",
        "research_agent/storage.py",
        "research_agent/trace.py",
    )
)


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def estimate_tokens(value: Any) -> int:
    return math.ceil(len(canonical_bytes(value)) / 3)


def check(name: str, actual: Any, expected: Any) -> dict[str, Any]:
    return {"name": name, "actual": actual, "expected": expected, "passed": actual == expected}


def predicate(name: str, actual: Any, expectation: str, passed: bool) -> dict[str, Any]:
    return {"name": name, "actual": actual, "expected": expectation, "passed": bool(passed)}


def row(probe_id: str, assertions: list[dict[str, Any]], measurements: dict[str, Any] | None = None) -> dict[str, Any]:
    numerator = sum(bool(item["passed"]) for item in assertions)
    return {
        "id": probe_id,
        "passed": numerator == len(assertions),
        "score": {"numerator": numerator, "denominator": len(assertions)},
        "assertions": assertions,
        "measurements": measurements or {},
    }


def failed_row(probe_id: str, exc: Exception, measurements: dict[str, Any] | None = None) -> dict[str, Any]:
    return row(
        probe_id,
        [predicate("probe_completed", f"{type(exc).__name__}: {exc}", "no exception", False)],
        measurements,
    )


def _read_response(content: str, **metadata: Any) -> ReadResponse:
    supported = inspect.signature(ReadResponse).parameters
    values = {"ok": True, "url": SOURCE_URL, "content": content, **metadata}
    return ReadResponse(**{key: value for key, value in values.items() if key in supported})


def _evidence(evidence_id: str, source_id: str, content: str, **metadata: Any) -> Evidence:
    supported = inspect.signature(Evidence).parameters
    values = {"evidence_id": evidence_id, "source_id": source_id, "content": content, **metadata}
    return Evidence(**{key: value for key, value in values.items() if key in supported})


def _public_addresses() -> list[tuple[Any, ...]]:
    import socket

    return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 443))]


def html_extraction_probe() -> dict[str, Any]:
    raw = HTML_FIXTURE.encode("utf-8")
    try:
        with (
            patch.object(search_module, "resolve_public_addresses", return_value=_public_addresses()),
            patch.object(
                search_module,
                "_request_once",
                return_value=search_module._NetworkResponse(
                    200,
                    {"content-type": "text/html; charset=utf-8"},
                    raw,
                    False,
                ),
            ),
        ):
            response = HttpReader(max_bytes=len(raw) + 1).read(SOURCE_URL)
        content = response.content
        content_hash = getattr(response, "content_hash", "")
        markers = {
            marker: marker in content
            for marker in (
                "Evidence Budget Guide",
                "KEEP_ENGLISH_SENTINEL",
                "KEEP_SEMANTIC_HEADER_SENTINEL",
                "保留中文正文",
                "KEEP_LIST_SENTINEL",
                "KEEP_DIV_SENTINEL",
                "KEEP_TABLE_HEADER",
                "KEEP_TABLE_METRIC",
                "KEEP_NESTED_SENTINEL",
                "KEEP_NESTED_TAIL",
                "def keep_pair(value)",
                "[S1]-[E1]",
                "KEEP_TAIL_SENTINEL",
            )
        }
        noise = {
            marker: marker in content
            for marker in (
                "NOISE_STYLE_SENTINEL",
                "NOISE_SCRIPT_SENTINEL",
                "NOISE_HEADER_SENTINEL",
                "NOISE_NAV_SENTINEL",
                "NOISE_NESTED_HEADER_SENTINEL",
                "NOISE_ASIDE_SENTINEL",
                "NOISE_FOOTER_SENTINEL",
            )
        }
        measurements = {
            "raw_bytes": len(raw),
            "content_bytes": len(content.encode("utf-8")),
            "retained_markers": markers,
            "noise_markers_present": noise,
            "title": getattr(response, "title", None),
            "content_hash": content_hash or None,
            "truncated": getattr(response, "truncated", None),
            "content_preview": content[:500],
            "duplicate_paragraph_count": content.count("KEEP_DUPLICATE_SENTINEL"),
        }
        return row("html_extraction", [
            check("read_ok", response.ok, True),
            check("title", getattr(response, "title", None), "Structured Evidence Guide"),
            predicate("all_body_markers_retained", markers, "all true", all(markers.values())),
            predicate("all_noise_removed", noise, "all false", not any(noise.values())),
            check("semantic_header_retained", markers["KEEP_SEMANTIC_HEADER_SENTINEL"], True),
            check("nested_header_noise_removed", noise["NOISE_NESTED_HEADER_SENTINEL"], False),
            check("duplicate_paragraphs_preserved", content.count("KEEP_DUPLICATE_SENTINEL"), 2),
            predicate("markup_removed", "<script" in content or "<nav" in content, "false", "<script" not in content and "<nav" not in content),
            predicate("content_reduced", measurements["content_bytes"], f"< {len(raw)}", measurements["content_bytes"] < len(raw)),
            check("content_hash", content_hash, sha256(content.encode("utf-8"))),
            check("truncated", getattr(response, "truncated", None), False),
            check("media_type", response.media_type, "text/html"),
            check("final_url", response.final_url, SOURCE_URL),
        ], measurements)
    except Exception as exc:
        return failed_row("html_extraction", exc, {"raw_bytes": len(raw)})


def truncation_probe() -> dict[str, Any]:
    raw = ("<html><head><title>Bounded Read</title></head><body><p>KEEP_START " + "x" * 500 + "</p></body></html>").encode()
    limited = raw[: CONFIG["http_truncation_bytes"]]
    try:
        with (
            patch.object(search_module, "resolve_public_addresses", return_value=_public_addresses()),
            patch.object(
                search_module,
                "_request_once",
                return_value=search_module._NetworkResponse(
                    200,
                    {"content-type": "text/html; charset=utf-8"},
                    limited,
                    True,
                ),
            ),
        ):
            response = HttpReader(max_bytes=CONFIG["http_truncation_bytes"]).read(SOURCE_URL)
        content_hash = getattr(response, "content_hash", "")
        measurements = {
            "available_bytes": len(raw),
            "read_limit_bytes": CONFIG["http_truncation_bytes"],
            "received_bytes": len(limited),
            "content_bytes": len(response.content.encode("utf-8")),
            "truncated": getattr(response, "truncated", None),
            "title": getattr(response, "title", None),
            "content_hash": content_hash or None,
        }
        return row("explicit_truncation", [
            check("read_ok", response.ok, True),
            check("truncated", getattr(response, "truncated", None), True),
            check("title", getattr(response, "title", None), "Bounded Read"),
            check("content_hash", content_hash, sha256(response.content.encode("utf-8"))),
            predicate("limit_was_exercised", len(raw), f"> {len(limited)}", len(raw) > len(limited)),
        ], measurements)
    except Exception as exc:
        return failed_row("explicit_truncation", exc, {"available_bytes": len(raw), "received_bytes": len(limited)})


class SequenceModel:
    name = "h3-sequence-model"

    def __init__(
        self,
        decisions: list[ModelDecision],
        fact_citations: tuple[tuple[str, str, str], ...] = (),
    ):
        self.decisions = list(decisions)
        self.fact_citations = fact_citations
        self.requests: list[dict[str, Any]] = []

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ModelDecision:
        self.requests.append({"messages": copy.deepcopy(messages), "tools": copy.deepcopy(tools)})
        if self.decisions:
            return self.decisions.pop(0)
        if not self.fact_citations:
            raise AssertionError("unexpected model call")
        visible = "\n".join(
            str(message.get("content", ""))
            for message in messages
            if message.get("role") in {"assistant", "tool"}
        )
        facts = dict(re.findall(r"H3_FACT:([A-Z]+)=([A-Z0-9_-]+)", visible))
        if not all(label in facts for label, _, _ in self.fact_citations):
            return ModelDecision("final", "INSUFFICIENT: required facts are not visible in the supplied evidence.")
        return ModelDecision("final", "\n".join(
            f"{label}={facts[label]} [{source_id}] [{evidence_id}]"
            for label, source_id, evidence_id in self.fact_citations
        ))


class QuerySearch:
    def search(self, query: str) -> SearchResponse:
        suffix = "one" if "one" in query else "two"
        return SearchResponse(True, query, [{
            "title": f"Source {suffix}",
            "url": f"https://example.com/{suffix}",
            "snippet": f"Search result for source {suffix}. " + suffix * 800,
        }])


class MetadataReader:
    def __init__(self, bodies: dict[str, str], summary_chars: int | None = None):
        self.bodies = bodies
        self.summary_chars = summary_chars

    def read(self, url: str) -> ReadResponse:
        body = self.bodies[url]
        label = "ONE" if url.endswith("/one") else "TWO"
        return _read_response(
            body,
            summary=(
                body[:self.summary_chars] + "\n[summary truncated]"
                if self.summary_chars is not None and len(body) > self.summary_chars
                else body
                if self.summary_chars is not None
                else "Concise source summary without the reported measurement."
            ),
            title=f"Source {label.lower()}",
            content_hash=sha256(body.encode("utf-8")),
            truncated=False,
            retrieved_at="2026-09-14T00:00:00+00:00",
            media_type="text/plain",
            final_url=url,
        )


def _unwrap_last_tool(request: dict[str, Any]) -> dict[str, Any]:
    for message in reversed(request["messages"]):
        if message.get("role") != "tool":
            continue
        payload = json.loads(str(message.get("content", "{}")))
        return payload.get("UNTRUSTED_TOOL_DATA", payload)
    return {}


def evidence_separation_probe() -> dict[str, Any]:
    value = "VISIBLE_7319"
    body = "FULL_BODY_START\n" + "background evidence\n" * 130 + f"H3_FACT:SINGLE={value}\nFULL_BODY_TAIL_SENTINEL"
    model = SequenceModel([
        ModelDecision("tool_call", query="one", call_id="search-one"),
        ModelDecision("tool_call", tool_name="read", url="https://example.com/one", call_id="read-one"),
    ], (("SINGLE", "S1", "E2"),))
    try:
        with tempfile.TemporaryDirectory() as tmp:
            result = ResearchAgent(QuerySearch(), model, tmp, max_tool_calls=2, reader=MetadataReader({"https://example.com/one": body})).run("question?")
        page = next(item for item in result.evidence if item.kind == "page")
        payload = _unwrap_last_tool(model.requests[-1])
        model_content = str(payload.get("content", ""))
        summary = getattr(page, "summary", "")
        request_tokens = estimate_tokens(model.requests[-1])
        input_limit = 16_384 - 2_048 - 256
        measurements = {
            "reader_content_bytes": len(body.encode("utf-8")),
            "stored_content_bytes": len(page.content.encode("utf-8")),
            "stored_summary_bytes": len(summary.encode("utf-8")),
            "model_content_bytes": len(model_content.encode("utf-8")),
            "stored_tail": page.content.endswith("FULL_BODY_TAIL_SENTINEL"),
            "model_tail": "FULL_BODY_TAIL_SENTINEL" in model_content,
            "result_status": result.status,
            "fact_offset": body.index("H3_FACT:SINGLE"),
            "final_request_estimated_tokens": request_tokens,
            "input_limit_tokens": input_limit,
            "answer": result.answer,
        }
        return row("evidence_separation", [
            check("full_content_exact", page.content, body),
            check("full_content_tail", page.content.endswith("FULL_BODY_TAIL_SENTINEL"), True),
            predicate("summary_nonempty", len(summary), "> 0", bool(summary.strip())),
            predicate("summary_smaller", len(summary.encode("utf-8")), f"< {len(body.encode('utf-8'))}", 0 < len(summary.encode("utf-8")) < len(body.encode("utf-8"))),
            check("model_receives_full_body", model_content, body),
            check("model_receives_late_fact", value in model_content, True),
            predicate("fact_is_beyond_old_prefix", body.index("H3_FACT:SINGLE"), "> 2014", body.index("H3_FACT:SINGLE") > 2014),
            predicate("request_within_input_limit", request_tokens, f"<= {input_limit}", request_tokens <= input_limit),
            check("dynamic_answer_from_visible_fact", value in result.answer and result.status == "ok", True),
            check("title_forwarded", getattr(page, "title", None), "Source one"),
            check("hash_forwarded", getattr(page, "content_hash", None), sha256(body.encode("utf-8"))),
            check("truncation_forwarded", getattr(page, "truncated", None), False),
        ], measurements)
    except Exception as exc:
        return failed_row("evidence_separation", exc, {"reader_content_bytes": len(body.encode("utf-8")), "model_calls": len(model.requests)})


def credential_redaction_probe() -> dict[str, Any]:
    secrets = ("access-value", "refresh-value", "client-value", "key-value", "auth-value", "proxy-value")
    try:
        edge_secrets = {
            "upper": "upper-secret-value",
            "password": "database-password-value",
            "leading": "leading-secret-value",
            "escaped": "escaped-secret-value",
            "long": "long-key-secret-value",
            "nested_access": "nested-access-value",
            "nested_client": "nested-client-value",
            "nested_key": "nested-key-value",
            "inner_quote": "TAIL_SECRET_4831",
            "delimiters": "DELIMITER_SECRET_5927",
            "placeholder": "PLACEHOLDER_SECRET_8264",
            "placeholder_nested": "NESTED_PLACEHOLDER_SECRET_9257",
        }
        long_key = "a" * 129 + "_token"
        escaped_json = f'{{\\"accessToken\\":\\"{edge_secrets["escaped"]}\\"}}'
        edge_assignments = (
            f'_access_token={edge_secrets["leading"]} -api_key={edge_secrets["leading"]} '
            f'{long_key}={edge_secrets["long"]} {escaped_json}'
        )
        nested_assignments = (
            f'Provider response: {{"accessToken":"{edge_secrets["nested_access"]}"}} '
            f'error={{"clientSecret":"{edge_secrets["nested_client"]}"}} '
            f'payload: accessToken={edge_secrets["nested_access"]} '
            f'headers: {{"X-Api-Key":"{edge_secrets["nested_key"]}"}} '
            f'{{"password":"alpha\\"{edge_secrets["inner_quote"]}"}} '
            f'{{\\"password\\":\\"alpha\\\\\\"{edge_secrets["inner_quote"]}\\"}}'
        )
        delimiter_assignments = " ".join(
            f"password=alpha{delimiter}{edge_secrets['delimiters']}"
            for delimiter in (",", "}", "]", '"', r'\"')
        )
        placeholder_assignments = " ".join(
            f"{key}[REDACTED]{delimiter}{edge_secrets['placeholder']}"
            for key in ("password=", "Authorization: ", "Authorization: Bearer ", "Authorization: Digest ")
            for delimiter in ("", ",", "}", "]", '"', r'\"')
        )
        placeholder_nested_assignments = (
            f'password=[REDACTED],token="alpha {edge_secrets["placeholder_nested"]}" '
            f'{{"message":"password=[REDACTED]","token":"alpha {edge_secrets["placeholder_nested"]}"}} '
            f'Authorization: Bearer abc,"password":"alpha {edge_secrets["placeholder_nested"]}"'
        )
        ordinary_text = "Press key=Enter. The key: finding. MONKEY=value."
        unterminated = tuple(
            f"password={opener}UNTERMINATED_SECRET_6109"
            for opener in ('"', "'", r'\"', r"\'")
        )
        unterminated_redacted = [redact(item) for item in unterminated]
        redacted = redact({
            "accessToken": secrets[0],
            "message": f"refreshToken={secrets[1]} clientSecret={secrets[2]} {edge_assignments} {nested_assignments}",
            "key": secrets[3],
            "raw": f"authorization={secrets[4]}&key={secrets[3]}",
            "ACCESSTOKEN": secrets[0],
            "GITHUBTOKEN": edge_secrets["upper"],
            "DATABASEPASSWORD": edge_secrets["password"],
            "proxyAuthorization": secrets[5],
            "promptTokens": 12,
        })
        timings = []
        redact("warmup")
        for repetitions in (2000, 4000, 8000):
            samples = []
            for _ in range(3):
                started = time.perf_counter()
                redact("a_" * repetitions + "x")
                samples.append(time.perf_counter() - started)
            timings.append(min(samples))
        growth_ratio = timings[-1] / timings[1]
        final_secret = "agent-secret-value"
        final_payload = (
            f"accessToken={final_secret} GITHUBTOKEN={edge_secrets['upper']} "
            f"{edge_assignments} {nested_assignments} {delimiter_assignments} {placeholder_assignments} "
            f"{placeholder_nested_assignments} {ordinary_text}"
        )
        model = SequenceModel([ModelDecision("final", final_payload)])
        with tempfile.TemporaryDirectory() as tmp:
            result = ResearchAgent(QuerySearch(), model, tmp, max_tool_calls=0).run("hello")
            trace_text = Path(result.trace_path).read_text(encoding="utf-8")
        json_inputs = [
            prefix + encoded + suffix
            for item in PROBE_DATA["credential_json_cases"]
            for plain in [json.dumps(item, separators=(",", ":"))]
            for encoding in PROBE_DATA["credential_json_encodings"]
            for encoded in [{"plain": plain, "string": json.dumps(plain), "escaped": json.dumps(plain)[1:-1]}[encoding]]
            for prefix, suffix in PROBE_DATA["credential_json_wrappers"]
        ]
        json_expected = [item.replace("JSON_TAIL_SECRET", "").replace("sk-synthetic_key_927461", "sk-[REDACTED]")
                         for item in json_inputs]
        json_answers, json_traces = [], []
        for source in json_inputs:
            with tempfile.TemporaryDirectory() as tmp:
                json_result = ResearchAgent(QuerySearch(), SequenceModel([ModelDecision("final", source)]), tmp, max_tool_calls=0).run("hello")
                events = [json.loads(line) for line in Path(json_result.trace_path).read_text(encoding="utf-8").splitlines()]
            json_answers.append({"status": json_result.status, "answer": json_result.answer})
            json_traces.append([
                event.get("raw_text_truncated") if event["event"] == "model_response" else event.get("answer")
                for event in events if event["event"] in {"model_response", "final_validated"}
            ])
        serialized = json.dumps(redacted, sort_keys=True)
        measurements = {
            "input_chars": [4001, 8001, 16001],
            "elapsed_seconds": timings,
            "doubling_growth_ratio": growth_ratio,
            "redacted": redacted,
            "final_answer": result.answer,
            "ordinary_text": ordinary_text,
            "unterminated_redacted": unterminated_redacted,
            "json_answers": json_answers,
            "json_traces": json_traces,
            "secret_leaks": {
                "structured": [secret for secret in edge_secrets.values() if secret in serialized],
                "final": [secret for secret in (final_secret, *edge_secrets.values()) if secret in result.answer],
                "trace": [secret for secret in (final_secret, *edge_secrets.values()) if secret in trace_text],
            },
        }
        return row("credential_redaction", [
            check("camel_case_values_removed", all(secret not in serialized for secret in secrets), True),
            check("edge_case_values_removed", all(secret not in serialized for secret in edge_secrets.values()), True),
            check("nested_assignments_removed", all(
                secret not in serialized and secret not in result.answer and secret not in trace_text
                for secret in (edge_secrets["nested_access"], edge_secrets["nested_client"], edge_secrets["nested_key"])
            ), True),
            check("escaped_inner_quote_removed", all(
                edge_secrets["inner_quote"] not in surface
                for surface in (serialized, result.answer, trace_text)
            ), True),
            check("unquoted_delimiter_suffixes_removed", all(
                edge_secrets["delimiters"] not in surface
                for surface in (result.answer, trace_text)
            ), True),
            check("placeholder_suffixes_removed", all(
                edge_secrets["placeholder"] not in surface
                for surface in (redact(placeholder_assignments), result.answer, trace_text)
            ), True),
            check("placeholder_redaction_idempotent", redact(redact(placeholder_assignments)), redact(placeholder_assignments)),
            check("placeholder_nested_assignments_removed", all(
                edge_secrets["placeholder_nested"] not in surface
                for surface in (redact(placeholder_nested_assignments), result.answer, trace_text)
            ), True),
            check("json_ordinary_fields_preserved", [redact(item) for item in json_inputs], json_expected),
            check("json_answer_fields_preserved", json_answers, [{"status": "ok", "answer": item} for item in json_expected]),
            check("json_trace_fields_preserved", json_traces, [[item, item] for item in json_expected]),
            check("telemetry_preserved", redacted.get("promptTokens"), 12),
            check("uppercase_credential_url_blocked", search_module.canonical_url(f"https://example.com/?GITHUBTOKEN={edge_secrets['upper']}") is None, True),
            check("final_answer_redacted", all(secret not in result.answer for secret in (final_secret, *edge_secrets.values())), True),
            check("trace_redacted", all(secret not in trace_text for secret in (final_secret, *edge_secrets.values())), True),
            check("ordinary_answer_text_preserved", ordinary_text in result.answer, True),
            check("ordinary_redaction_text_preserved", redact(ordinary_text), ordinary_text),
            check("unterminated_credentials_removed", all(
                "UNTERMINATED_SECRET_6109" not in item for item in unterminated_redacted
            ), True),
            predicate("large_input_under_half_second", timings[-1], "< 0.5", timings[-1] < 0.5),
            predicate("doubling_growth_ratio_under_3x", growth_ratio, "< 3.0", growth_ratio < 3.0),
        ], measurements)
    except Exception as exc:
        return failed_row("credential_redaction", exc)


def _create_legacy_database(path: Path) -> None:
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.executescript("""
        CREATE TABLE runs (id TEXT PRIMARY KEY, question TEXT NOT NULL, answer TEXT NOT NULL, status TEXT NOT NULL, termination TEXT NOT NULL, tool_calls INTEGER NOT NULL, trace_path TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE evidence (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE, evidence_id TEXT NOT NULL, source_id TEXT NOT NULL, content TEXT NOT NULL, kind TEXT NOT NULL, retrieved_at TEXT NOT NULL);
        INSERT INTO runs VALUES('legacy','q','a','ok','model_final',0,'legacy.jsonl','2026-01-01T00:00:00Z');
        INSERT INTO evidence(run_id,evidence_id,source_id,content,kind,retrieved_at) VALUES('legacy','E1','S1','legacy body','page','2026-01-01T00:00:00Z');
        """)


def sqlite_migration_probe() -> dict[str, Any]:
    required = {"summary", "title", "content_hash", "truncated"}
    try:
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "legacy.db"
            _create_legacy_database(database)
            store = RunStore(database)
            with closing(sqlite3.connect(database)) as conn:
                columns = {row[1] for row in conn.execute("PRAGMA table_info(evidence)")}
                legacy = conn.execute("SELECT evidence_id,content FROM evidence WHERE run_id='legacy'").fetchone()

            trace = Path(tmp) / "new-run.jsonl"
            trace.write_text("{}\n", encoding="utf-8")
            body = "new complete body"
            enhanced = _evidence(
                "E1",
                "S1",
                body,
                summary="new summary",
                title="New title",
                content_hash=sha256(body.encode()),
                truncated=True,
                retrieved_at="2026-09-14T00:00:00Z",
                kind="page",
            )
            result = RunResult(
                answer="answer",
                sources=[],
                trace_path=str(trace),
                status="ok",
                termination="model_final",
                tool_calls=0,
                valid_citations=[],
                invalid_citations=[],
                evidence=[enhanced],
            )
            run_id = store.save(result, "question", "2026-09-14T00:00:00Z")
            loaded = store.get(run_id) or {}
            loaded_evidence = (loaded.get("evidence") or [{}])[0]
        measurements = {
            "columns": sorted(columns),
            "legacy_row": list(legacy) if legacy else None,
            "new_evidence": loaded_evidence,
        }
        return row("sqlite_compatible_migration", [
            predicate("columns_added", sorted(required - columns), "empty", required <= columns),
            check("legacy_row_preserved", list(legacy) if legacy else None, ["E1", "legacy body"]),
            check("new_summary_roundtrip", loaded_evidence.get("summary"), "new summary"),
            check("new_title_roundtrip", loaded_evidence.get("title"), "New title"),
            check("new_hash_roundtrip", loaded_evidence.get("content_hash"), sha256(body.encode())),
            predicate("new_truncated_roundtrip", loaded_evidence.get("truncated"), "true/1", loaded_evidence.get("truncated") in (True, 1)),
        ], measurements)
    except Exception as exc:
        return failed_row("sqlite_compatible_migration", exc)


def _tool_pair(call_id: str, marker: str, filler: str, *, error: str = "") -> list[dict[str, Any]]:
    payload: dict[str, Any] = {"ok": not error, "marker": marker, "content": filler}
    if error:
        payload["error"] = {"code": "read_transient", "message": error}
    return [
        {
            "role": "assistant",
            "tool_calls": [{
                "id": call_id,
                "type": "function",
                "function": {"name": "read", "arguments": json.dumps({"url": f"https://example.com/{call_id}"})},
            }],
        },
        {"role": "tool", "tool_call_id": call_id, "content": json.dumps({"UNTRUSTED_TOOL_DATA": payload})},
    ]


def protocol_errors(messages: list[dict[str, Any]]) -> list[str]:
    calls: dict[str, int] = {}
    results: dict[str, int] = {}
    errors: list[str] = []
    for index, message in enumerate(messages):
        if message.get("role") == "assistant" and message.get("tool_calls"):
            for call in message["tool_calls"]:
                call_id = str(call.get("id", ""))
                calls[call_id] = calls.get(call_id, 0) + 1
        if message.get("role") == "tool":
            call_id = str(message.get("tool_call_id", ""))
            results[call_id] = results.get(call_id, 0) + 1
            if index == 0 or messages[index - 1].get("role") != "assistant" or not any(
                str(item.get("id", "")) == call_id for item in messages[index - 1].get("tool_calls", [])
            ):
                errors.append(f"orphan tool result {call_id}")
    for call_id in sorted(set(calls) | set(results)):
        if calls.get(call_id) != 1 or results.get(call_id) != 1:
            errors.append(f"unpaired {call_id}: calls={calls.get(call_id, 0)} results={results.get(call_id, 0)}")
    return errors


def structured_context_probe() -> dict[str, Any]:
    question = "中文上下文必须保留。Evaluate code: def 保留(value): return '[S1]-[E1]'"
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT + "\nSYSTEM-CONTEXT-SENTINEL"},
        {"role": "user", "content": question},
    ]
    for index in range(1, 5):
        messages.extend(_tool_pair(
            f"call-{index}",
            f"GROUP_{index}_SENTINEL",
            f"old-group-{index}-" + str(index) * 2500,
            error="latest-error-sentinel" if index == 4 else "",
        ))
    sources = [
        Source("S1", "Primary", "https://example.com/one", "alpha"),
        Source("S2", "Counter", "https://example.com/two", "beta"),
    ]
    evidence = [
        _evidence("E1", "S1", "alpha full", summary="alpha summary", title="Primary", content_hash=sha256(b"alpha full"), truncated=False),
        _evidence("E2", "S2", "beta full", summary="beta summary", title="Counter", content_hash=sha256(b"beta full"), truncated=False),
    ]
    claims = [
        Claim("C-open", "未解决 Claim 需要 E1", ["E1"], "INSUFFICIENT", None, "missing detail"),
        Claim("C-conflict", "冲突 Claim links E1 and E2", ["E1", "E2"], "CONFLICTING", None, "sources disagree"),
    ]
    before_bytes = len(canonical_bytes({"messages": messages, "tools": TOOL_SCHEMA}))
    try:
        build_context = importlib.import_module("research_agent.context").build_context
        built = build_context(
            messages,
            tools=TOOL_SCHEMA,
            question=question,
            sources=sources,
            evidence=evidence,
            claims=claims,
            **CONFIG["direct_context"],
        )
        output = built.messages
        serialized = canonical_bytes({"messages": output, "tools": TOOL_SCHEMA}).decode("utf-8")
        errors = protocol_errors(output)
        measurements = {
            "before_bytes_runner": before_bytes,
            "after_bytes_runner": len(serialized.encode("utf-8")),
            "result_before_bytes": built.before_bytes,
            "result_after_bytes": built.after_bytes,
            "before_estimated_tokens": built.before_estimated_tokens,
            "estimated_input_tokens": built.estimated_input_tokens,
            "input_limit_tokens": built.input_limit_tokens,
            "estimate_method": built.estimate_method,
            "retained": built.retained,
            "removed": built.removed,
            "elapsed_ms": built.elapsed_ms,
            "protocol_errors": errors,
            "roles": [item.get("role") for item in output],
        }
        assertions = [
            check("error", built.error, ""),
            check("system_retained", "SYSTEM-CONTEXT-SENTINEL" in serialized, True),
            check("question_retained", question in serialized, True),
            check("chinese_retained", "中文上下文必须保留" in serialized, True),
            check("code_retained", "def 保留(value): return '[S1]-[E1]'" in serialized, True),
            check("unresolved_claim_retained", "C-open" in serialized and "未解决 Claim" in serialized, True),
            check("conflict_retained", "C-conflict" in serialized and "CONFLICTING" in serialized and "E1" in serialized and "E2" in serialized, True),
            check("source_evidence_mapping_retained", all(marker in serialized for marker in ("S1", "S2", "E1", "E2")), True),
            check("latest_error_retained", "latest-error-sentinel" in serialized, True),
            check("last_two_pairs_retained", all(marker in serialized for marker in ("call-3", "call-4", "GROUP_3_SENTINEL", "GROUP_4_SENTINEL")), True),
            check("old_full_groups_removed", "old-group-1-" in serialized or "old-group-2-" in serialized, False),
            check("protocol_pairs", errors, []),
            predicate("bytes_strictly_reduced", built.after_bytes, f"< {built.before_bytes}", built.after_bytes < built.before_bytes),
            predicate("tokens_strictly_reduced", built.estimated_input_tokens, f"< {built.before_estimated_tokens}", built.estimated_input_tokens < built.before_estimated_tokens),
            predicate("within_input_limit", built.estimated_input_tokens, f"<= {built.input_limit_tokens}", built.estimated_input_tokens <= built.input_limit_tokens),
            predicate("estimate_disclosed", built.estimate_method, "non-empty", bool(str(built.estimate_method).strip())),
            predicate("retention_accounted", built.retained, "non-empty", bool(built.retained)),
            predicate("removal_accounted", built.removed, "non-empty", bool(built.removed)),
            predicate("elapsed_recorded", built.elapsed_ms, ">= 0", isinstance(built.elapsed_ms, (int, float)) and built.elapsed_ms >= 0),
        ]
        return row("structured_context", assertions, measurements)
    except Exception as exc:
        return failed_row("structured_context", exc, {"before_bytes_runner": before_bytes, "before_estimated_tokens_runner": estimate_tokens({"messages": messages, "tools": TOOL_SCHEMA})})


def short_read_budget_probe() -> dict[str, Any]:
    fixture = PROBE_DATA["short_read_budget"]
    head = [
        {"role": "system", "content": "rules" + "s" * fixture["system_padding_chars"]},
        {"role": "user", "content": "question"},
    ]
    pairs = [
        [
            {"role": "assistant", "tool_calls": [{
                "id": f"read-{index}", "type": "function",
                "function": {"name": "read", "arguments": "{}"},
            }]},
            {"role": "tool", "tool_call_id": f"read-{index}", "content": json.dumps({
                "UNTRUSTED_TOOL_DATA": {"kind": "read", "evidence_id": f"E{index}", "content": "x" * length},
            }, ensure_ascii=False)},
        ]
        for index, length in enumerate(fixture["body_chars"], 1)
    ]
    messages = head + [message for pair in pairs for message in pair]
    expected = head + pairs[-2] + pairs[-1]
    expected_tokens = estimate_tokens({"messages": expected, "tools": []})
    try:
        build_context = importlib.import_module("research_agent.context").build_context
        built = build_context(
            messages, max_context_tokens=CONFIG["short_read_budget_tokens"],
            output_reserve_tokens=0, safety_margin_tokens=0, question="question",
        )
        return row("short_read_budget", [
            check("full_recent_pairs_fit", expected_tokens, 693),
            check("error", built.error, ""),
            check("short_bodies_preserved", built.messages, expected),
            check("actual_size_recorded", built.estimated_input_tokens, expected_tokens),
            check("only_old_pair_removed", built.removed, ["tool_pair:read-1"]),
            check("protocol_pairs", protocol_errors(built.messages), []),
        ], {
            "full_recent_pairs_estimated_tokens": expected_tokens,
            "estimated_input_tokens": built.estimated_input_tokens,
            "input_limit_tokens": built.input_limit_tokens,
            "error": built.error, "removed": built.removed,
        })
    except Exception as exc:
        return failed_row("short_read_budget", exc)


def _agent_with_context(search: Any, model: Any, trace_dir: str, **kwargs: Any) -> ResearchAgent:
    supported = inspect.signature(ResearchAgent).parameters
    context_keys = {"max_context_tokens", "output_reserve_tokens", "context_safety_margin_tokens"}
    usable = {key: value for key, value in kwargs.items() if key in supported or key not in context_keys}
    return ResearchAgent(search, model, trace_dir, **usable)


def _loop_scenario(*, include_facts: bool = True) -> tuple[RunResult, SequenceModel, list[dict[str, Any]]]:
    facts = {
        "one": "\nReported measurement from source one.\nH3_FACT:ONE=ORBIT_17\n",
        "two": "\nReported measurement from source two.\nH3_FACT:TWO=QUARTZ_29\n",
    }
    bodies = {
        "https://example.com/one": "ONE_BODY_START\n" + "alpha background\n" * 650 + (facts["one"] if include_facts else "") + "ONE_BODY_TAIL",
        "https://example.com/two": "TWO_BODY_START\n" + "beta background\n" * 650 + (facts["two"] if include_facts else "") + "TWO_BODY_TAIL",
    }
    model = SequenceModel([
        ModelDecision("tool_call", query="one", call_id="search-one"),
        ModelDecision("tool_call", tool_name="read", url="https://example.com/one", call_id="read-one"),
        ModelDecision("tool_call", query="two", call_id="search-two"),
        ModelDecision("tool_call", tool_name="read", url="https://example.com/two", call_id="read-two"),
    ], (("ONE", "S1", "E2"), ("TWO", "S2", "E4")))
    with tempfile.TemporaryDirectory() as tmp:
        agent = _agent_with_context(
            QuerySearch(),
            model,
            tmp,
            max_tool_calls=4,
            max_rounds=7,
            reader=MetadataReader(bodies),
            max_context_tokens=CONFIG["loop_context"]["max_context_tokens"],
            output_reserve_tokens=CONFIG["loop_context"]["output_reserve_tokens"],
            context_safety_margin_tokens=CONFIG["loop_context"]["safety_margin_tokens"],
        )
        result = agent.run("Compare the reported measurements from both sources with evidence.")
        trace_rows = [json.loads(line) for line in Path(result.trace_path).read_text(encoding="utf-8").splitlines()]
    return result, model, trace_rows


def _prefix_cascade_scenario() -> tuple[RunResult, SequenceModel, tuple[int, int]]:
    facts = ("REGRESSION_A", "REGRESSION_B")
    bodies = {
        "https://example.com/one": "A_START\n" + "x" * 500 + f"\nH3_FACT:ONE={facts[0]}\n" + "x" * 8470 + "\nA_END",
        "https://example.com/two": "B_START\n" + "y" * 500 + f"\nH3_FACT:TWO={facts[1]}\n" + "y" * 8470 + "\nB_END",
    }
    model = SequenceModel([
        ModelDecision("tool_call", query="one", call_id="prefix-search-one"),
        ModelDecision("tool_call", tool_name="read", url="https://example.com/one", call_id="prefix-read-one"),
        ModelDecision("tool_call", query="two", call_id="prefix-search-two"),
        ModelDecision("tool_call", tool_name="read", url="https://example.com/two", call_id="prefix-read-two"),
    ], (("ONE", "S1", "E2"), ("TWO", "S2", "E4")))
    with tempfile.TemporaryDirectory() as tmp:
        result = ResearchAgent(
            QuerySearch(),
            model,
            tmp,
            max_tool_calls=4,
            max_rounds=7,
            reader=MetadataReader(bodies, summary_chars=1600),
            max_context_tokens=5000,
            output_reserve_tokens=700,
            context_safety_margin_tokens=300,
        ).run("Compare the reported measurements from both sources with evidence.")
    return result, model, tuple(body.index("H3_FACT") for body in bodies.values())


def loop_probes() -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        result, model, trace_rows = _loop_scenario()
        negative, negative_model, _ = _loop_scenario(include_facts=False)
        prefix_result, prefix_model, prefix_offsets = _prefix_cascade_scenario()
        request_bytes = [len(canonical_bytes(request)) for request in model.requests]
        request_tokens = [estimate_tokens(request) for request in model.requests]
        protocols = [protocol_errors(request["messages"]) for request in model.requests]
        negative_protocols = [protocol_errors(request["messages"]) for request in negative_model.requests]
        context_events = [item for item in trace_rows if item.get("event") == "context_built"]
        compacted = [item for item in context_events if item.get("after_bytes", 0) < item.get("before_bytes", 0)]
        final_request = json.dumps(model.requests[-1], ensure_ascii=False)
        negative_requests = json.dumps(negative_model.requests, ensure_ascii=False)
        prefix_final_request = json.dumps(prefix_model.requests[-1], ensure_ascii=False)
        prefix_tokens = estimate_tokens(prefix_model.requests[-1])
        prefix_protocol = protocol_errors(prefix_model.requests[-1]["messages"])
        values = ("ORBIT_17", "QUARTZ_29")
        fact_offsets = (
            len("ONE_BODY_START\n" + "alpha background\n" * 650),
            len("TWO_BODY_START\n" + "beta background\n" * 650),
        )
        leak_surface = "\n".join(
            str(message.get("content", ""))
            for request in model.requests
            for message in request["messages"]
            if message.get("role") == "user"
            or (message.get("role") == "tool" and '"kind": "read"' not in str(message.get("content", "")))
        )
        quality_assertions = [
            check("result_status", result.status, "ok"),
            check("termination", result.termination, "model_final"),
            check("sources", len(result.sources), 2),
            check("evidence", len(result.evidence), 4),
            check("valid_citations", result.valid_citations, ["S1", "S2"]),
            check("all_requests_protocol_valid", protocols, [[] for _ in protocols]),
            check("answer_facts_derived", all(marker in result.answer for marker in (*values, "[E2]", "[E4]")), True),
            check("facts_visible_to_model", all(value in final_request for value in values), True),
            check("facts_not_leaked_by_question_or_search", all(value not in leak_surface for value in values), True),
            check("late_fact_offsets", all(offset > 2014 for offset in fact_offsets), True),
            check("requests_within_limit", all(tokens <= CONFIG["loop_context"]["max_context_tokens"] - CONFIG["loop_context"]["output_reserve_tokens"] - CONFIG["loop_context"]["safety_margin_tokens"] for tokens in request_tokens), True),
            check("context_excerpt_disclosed", '\\\"context_excerpted\\\": true' in final_request, True),
            check("stripped_evidence_refuses", negative.status, "insufficient"),
            check("stripped_evidence_reaches_model_final", negative.termination, "model_final"),
            check("stripped_evidence_model_calls", len(negative_model.requests), 5),
            check("stripped_evidence_protocol_valid", negative_protocols, [[] for _ in negative_protocols]),
            check("stripped_evidence_sources", len(negative.sources), 2),
            check("stripped_evidence_records", len(negative.evidence), 4),
            check("stripped_evidence_has_no_fact_values", all(value not in negative.answer and value not in negative_requests for value in values), True),
            check("prefix_cascade_result_status", prefix_result.status, "ok"),
            check("prefix_cascade_facts_visible", all(value in prefix_final_request for value in ("REGRESSION_A", "REGRESSION_B")), True),
            check("prefix_cascade_answer_derived", all(value in prefix_result.answer for value in ("REGRESSION_A", "REGRESSION_B", "[E2]", "[E4]")), True),
            check("prefix_cascade_protocol_valid", prefix_protocol, []),
            predicate("prefix_cascade_within_limit", prefix_tokens, "<= 4000", prefix_tokens <= 4000),
            check("prefix_facts_were_in_prior_summary", all(offset < 1600 for offset in prefix_offsets), True),
        ]
        measurements = {
            "model_calls": len(model.requests),
            "request_bytes": request_bytes,
            "request_estimated_tokens": request_tokens,
            "total_model_input_bytes": sum(request_bytes),
            "total_model_input_estimated_tokens": sum(request_tokens),
            "peak_model_input_bytes": max(request_bytes, default=0),
            "peak_model_input_estimated_tokens": max(request_tokens, default=0),
            "protocol_errors": protocols,
            "context_event_count": len(context_events),
            "compacted_event_count": len(compacted),
            "context_events": context_events,
            "fact_offsets": fact_offsets,
            "result": {
                "status": result.status,
                "termination": result.termination,
                "source_count": len(result.sources),
                "evidence_count": len(result.evidence),
                "valid_citations": result.valid_citations,
                "answer": result.answer,
            },
            "negative_control": {
                "status": negative.status,
                "termination": negative.termination,
                "answer": negative.answer,
                "model_calls": len(negative_model.requests),
                "protocol_errors": negative_protocols,
                "source_count": len(negative.sources),
                "evidence_count": len(negative.evidence),
            },
            "prefix_cascade": {
                "status": prefix_result.status,
                "answer": prefix_result.answer,
                "final_request_estimated_tokens": prefix_tokens,
                "input_limit_tokens": 4000,
                "fact_offsets": prefix_offsets,
                "protocol_errors": prefix_protocol,
                "facts_visible": {
                    value: value in prefix_final_request
                    for value in ("REGRESSION_A", "REGRESSION_B")
                },
            },
        }
        quality = row("loop_quality_guard", quality_assertions, measurements)
        trace = row("loop_context_trace", [
            check("event_per_model_call", len(context_events), len(model.requests)),
            predicate("compression_observed", len(compacted), ">= 1", bool(compacted)),
            check("trace_has_estimate_method", all(bool(item.get("estimate_method")) for item in context_events), True),
            check("trace_has_limits", all(isinstance(item.get("input_limit_tokens"), int) for item in context_events), True),
            check("trace_has_retained_removed", all("retained" in item and "removed" in item for item in context_events), True),
            check("trace_protocol_valid", protocols, [[] for _ in protocols]),
        ], measurements)
        return quality, trace
    except Exception as exc:
        failure = {"model_calls": 0, "total_model_input_bytes": 0, "total_model_input_estimated_tokens": 0}
        return failed_row("loop_quality_guard", exc, failure), failed_row("loop_context_trace", exc, failure)


class CountingModel:
    name = "must-not-be-called"

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ModelDecision:
        self.calls += 1
        return ModelDecision("final", "should not run")


def protected_over_budget_probe() -> dict[str, Any]:
    model = CountingModel()
    try:
        params = inspect.signature(ResearchAgent).parameters
        missing = sorted({"max_context_tokens", "output_reserve_tokens", "context_safety_margin_tokens"} - set(params))
        if missing:
            raise TypeError(f"ResearchAgent missing token-budget parameters: {', '.join(missing)}")
        with tempfile.TemporaryDirectory() as tmp:
            result = ResearchAgent(
                QuerySearch(),
                model,
                tmp,
                max_context_tokens=40,
                output_reserve_tokens=20,
                context_safety_margin_tokens=10,
            ).run("PROTECTED_QUESTION " + "必须完整保留" * 500)
            trace_rows = [json.loads(line) for line in Path(result.trace_path).read_text(encoding="utf-8").splitlines()]
        context_events = [item for item in trace_rows if item.get("event") == "context_built"]
        measurements = {
            "model_calls": model.calls,
            "termination": result.termination,
            "status": result.status,
            "context_events": context_events,
        }
        return row("protected_over_budget", [
            check("model_not_called", model.calls, 0),
            check("termination", result.termination, "context_budget_exceeded"),
            check("status", result.status, "insufficient"),
            check("context_event_count", len(context_events), 1),
            check("trace_error", context_events[0].get("error") if context_events else None, "context_budget_exceeded"),
            check("question_not_logged", "PROTECTED_QUESTION" in json.dumps(context_events, ensure_ascii=False), False),
        ], measurements)
    except Exception as exc:
        return failed_row("protected_over_budget", exc, {"model_calls": model.calls})


def run_probes() -> list[dict[str, Any]]:
    quality, loop_trace = loop_probes()
    return [
        html_extraction_probe(),
        truncation_probe(),
        evidence_separation_probe(),
        credential_redaction_probe(),
        sqlite_migration_probe(),
        structured_context_probe(),
        short_read_budget_probe(),
        loop_trace,
        protected_over_budget_probe(),
        quality,
    ]


def _file_manifest(paths: tuple[Path, ...]) -> dict[str, Any]:
    files = []
    for path in paths:
        relative = path.relative_to(ROOT).as_posix()
        if path.is_file():
            content = path.read_bytes()
            files.append({"path": relative, "bytes": len(content), "sha256": sha256(content)})
        else:
            files.append({"path": relative, "missing": True})
    return {"files": files, "combined_sha256": sha256(canonical_bytes(files))}


def _git_metadata() -> dict[str, Any]:
    def git(*args: str) -> str:
        return subprocess.run(
            ["git", *args],
            cwd=ROOT,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        ).stdout.strip()

    status = git("status", "--short", "--untracked-files=all").splitlines()
    return {"head": git("rev-parse", "HEAD") or None, "dirty": bool(status), "status": status}


def _metadata() -> dict[str, Any]:
    runner = Path(__file__).resolve()
    tests = tuple(sorted((ROOT / "tests").glob("test_h3*.py")))
    environment = {
        "python": sys.version,
        "implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "executable": sys.executable,
    }
    return {
        "code": _file_manifest(CODE_FILES),
        "evaluation": _file_manifest((runner, *tests)),
        "data": {"sha256": sha256(canonical_bytes(PROBE_DATA)), "value": PROBE_DATA},
        "config": {"sha256": sha256(canonical_bytes(CONFIG)), "value": CONFIG},
        "environment": {"sha256": sha256(canonical_bytes(environment)), "value": environment},
        "git": _git_metadata(),
    }


def _metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    quality = next(item for item in rows if item["id"] == "loop_quality_guard")
    loop = next(item for item in rows if item["id"] == "loop_context_trace")["measurements"]
    return {
        "rows": {
            "numerator": sum(item["passed"] for item in rows),
            "denominator": len(rows),
        },
        "assertions": {
            "numerator": sum(item["score"]["numerator"] for item in rows),
            "denominator": sum(item["score"]["denominator"] for item in rows),
        },
        "quality": quality["score"],
        "cost": {
            key: loop.get(key, 0)
            for key in (
                "total_model_input_bytes",
                "total_model_input_estimated_tokens",
                "peak_model_input_bytes",
                "peak_model_input_estimated_tokens",
            )
        },
    }


def _gate(rows: list[dict[str, Any]], metrics: dict[str, Any], baseline: dict[str, Any] | None, metadata: dict[str, Any]) -> dict[str, Any]:
    required = {item["id"]: item["passed"] for item in rows}
    all_required = all(required.get(name, False) for name in CONFIG["required_rows"])
    comparison: dict[str, Any] = {
        "baseline_supplied": baseline is not None,
        "same_runner": None,
        "same_evaluation": None,
        "same_data": None,
        "same_config": None,
        "strict_feature_improvement": None,
        "quality_strictly_better": None,
        "no_prior_pass_regressions": None,
        "below_historical_pre_h3_bytes": None,
        "below_historical_pre_h3_tokens": None,
    }
    if baseline is not None:
        old_meta = baseline.get("metadata", {})
        old_metrics = baseline.get("metrics", {})
        old_rows = {item.get("id"): item for item in baseline.get("rows", [])}
        current_rows = {item.get("id"): item for item in rows}
        historical = CONFIG["historical_pre_h3_cost"]
        comparison.update({
            "same_runner": old_meta.get("evaluation", {}).get("files", [{}])[0].get("sha256") == metadata["evaluation"]["files"][0].get("sha256"),
            "same_evaluation": old_meta.get("evaluation", {}).get("combined_sha256") == metadata["evaluation"]["combined_sha256"],
            "same_data": old_meta.get("data", {}).get("sha256") == metadata["data"]["sha256"],
            "same_config": old_meta.get("config", {}).get("sha256") == metadata["config"]["sha256"],
            "strict_feature_improvement": metrics["rows"]["numerator"] > old_metrics.get("rows", {}).get("numerator", metrics["rows"]["numerator"]),
            "quality_strictly_better": metrics["quality"]["numerator"] > old_metrics.get("quality", {}).get("numerator", metrics["quality"]["numerator"]),
            "no_prior_pass_regressions": all(
                not item.get("passed") or current_rows.get(name, {}).get("passed")
                for name, item in old_rows.items()
            ),
            "below_historical_pre_h3_bytes": metrics["cost"]["total_model_input_bytes"] < historical["total_model_input_bytes"],
            "below_historical_pre_h3_tokens": metrics["cost"]["total_model_input_estimated_tokens"] < historical["total_model_input_estimated_tokens"],
        })
    comparison_passed = baseline is not None and all(value is True for value in comparison.values())
    return {
        "logic": "all required rows pass; identical evaluation/data/config; passed rows and evidence-derived quality strictly improve over the P2 baseline; no prior passing row regresses; total model-input cost stays below the frozen pre-H3 baseline",
        "required_rows": required,
        "all_required_rows_passed": all_required,
        "comparison": comparison,
        "stage_passed": bool(all_required and comparison_passed),
    }


def build_report(label: str, baseline: dict[str, Any] | None = None) -> dict[str, Any]:
    rows = run_probes()
    metadata = _metadata()
    metrics = _metrics(rows)
    return {
        "schema_version": SCHEMA_VERSION,
        "stage": "H3",
        "label": label,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "offline": True,
        "network_requests": 0,
        "metadata": metadata,
        "rows": rows,
        "metrics": metrics,
        "gate": _gate(rows, metrics, baseline, metadata),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--require-pass", action="store_true")
    args = parser.parse_args(argv)

    if args.output.exists():
        parser.error(f"refusing to overwrite existing report: {args.output}")
    baseline = json.loads(args.baseline.read_text(encoding="utf-8")) if args.baseline else None
    report = build_report(args.label, baseline)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    print(json.dumps({
        "output": str(args.output),
        "rows": report["metrics"]["rows"],
        "assertions": report["metrics"]["assertions"],
        "stage_passed": report["gate"]["stage_passed"],
    }, ensure_ascii=False))
    return int(args.require_pass and not report["gate"]["stage_passed"])


if __name__ == "__main__":
    raise SystemExit(main())
