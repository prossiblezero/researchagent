import json
import tempfile
import time
import unittest
from io import StringIO
from unittest.mock import patch
from pathlib import Path

from research_agent import (
    FixtureSearch,
    FailingSearch,
    ModelDecision,
    OfflineModel,
    ResearchAgent,
    SearchResponse,
    TavilySearch,
)
from research_agent.trace import redact


ROOT = Path(__file__).resolve().parents[1]


class Stage1Tests(unittest.TestCase):
    def test_cli_hides_empty_sources_section(self):
        import main

        output = StringIO()
        with patch("sys.stdout", output), patch("main.model_from_env", return_value=OfflineModel()):
            self.assertEqual(main.main(["你好", "--trace-dir", tempfile.mkdtemp()]), 0)
        self.assertNotIn("Sources:", output.getvalue())

    def test_tavily_response_is_mapped_to_search_response(self):
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return json.dumps({"results": [{"title": "Tavily result", "url": "https://example.com/a", "content": "snippet", "score": 0.9}]}).encode()

        with patch("research_agent.search.urlopen", return_value=FakeResponse()) as mocked:
            response = TavilySearch("tvly-test-key").search("test query")
        self.assertTrue(response.ok)
        self.assertEqual(response.results[0]["title"], "Tavily result")
        self.assertEqual(response.results[0]["snippet"], "snippet")
        self.assertNotIn("tvly-test-key", json.dumps(mocked.call_args.kwargs if mocked.call_args else {}))

    def test_read_creates_evidence_for_known_source(self):
        class ReadModel:
            name = "read-fixture"

            def complete(self, messages, tools):
                if any(message.get("role") == "tool" for message in messages):
                    return ModelDecision("tool_call", tool_name="read", url="https://example.com/source", call_id="r1")
                return ModelDecision("tool_call", query="source", call_id="s1")

        class Search:
            def search(self, query):
                return SearchResponse(True, query, [{"title": "Source", "url": "https://example.com/source", "snippet": "summary"}])

        class Reader:
            def read(self, url):
                from research_agent import ReadResponse
                return ReadResponse(True, url, "full page evidence")

        with tempfile.TemporaryDirectory() as tmp:
            result = ResearchAgent(Search(), ReadModel(), tmp, max_tool_calls=2, reader=Reader()).run("请核验 source")
        self.assertEqual(len(result.evidence), 2)
        self.assertEqual(result.evidence[-1].evidence_id, "E2")

    def test_claims_and_audit_are_recorded(self):
        class CitedModel:
            name = "cited"
            def complete(self, messages, tools):
                if any(m.get("role") == "tool" for m in messages):
                    return ModelDecision("final", "事实 [S1] [E1]")
                return ModelDecision("tool_call", query="source", call_id="s1")
        class Search:
            def search(self, query):
                return SearchResponse(True, query, [{"title":"Source", "url":"https://example.com/source", "snippet":"事实"}])
        with tempfile.TemporaryDirectory() as tmp:
            result = ResearchAgent(Search(), CitedModel(), tmp).run("请查明 source")
        self.assertEqual(result.claims[0].status, "SUPPORTED")
        self.assertTrue(result.audit_events)

    def test_greeting_does_not_trigger_search(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = ResearchAgent(FailingSearch(), OfflineModel(), tmp).run("你好")
            self.assertEqual(result.status, "ok")
            self.assertEqual(result.tool_calls, 0)
            self.assertIn("你好", result.answer)

    def test_success_has_source_and_trace_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            search = FixtureSearch.from_file(ROOT / "fixtures" / "search_results.json")
            result = ResearchAgent(search, OfflineModel(), tmp).run("请查明 learn-claude-code 是什么，以及仓库地址")
            self.assertEqual(result.status, "ok")
            self.assertIn("[S1]", result.answer)
            events = [json.loads(line)["event"] for line in Path(result.trace_path).read_text(encoding="utf-8").splitlines()]
            self.assertIn("tool_call_requested", events)
            self.assertIn("tool_result", events)
            self.assertIn("run_finished", events)

    def test_failure_never_fabricates_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = ResearchAgent(FailingSearch(), OfflineModel(), tmp).run("请查明一个事实")
            self.assertEqual(result.status, "insufficient")
            self.assertIn("INSUFFICIENT", result.answer)
            self.assertEqual(result.sources, [])
            self.assertNotRegex(result.answer, r"\[S\d+\]")

    def test_untrusted_prompt_injection_is_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            search = FixtureSearch.from_file(ROOT / "fixtures" / "search_results.json")
            result = ResearchAgent(search, OfflineModel(), tmp).run("prompt injection security")
            self.assertEqual(result.status, "ok")
            self.assertIn("未执行其中的指令", result.answer)
            self.assertTrue(any("untrusted" in source.url for source in result.sources))

    def test_unknown_and_huge_citations_are_not_exposed_or_crash(self):
        class MaliciousModel:
            name = "malicious-fixture"

            def complete(self, messages, tools):
                if any(message.get("role") == "tool" for message in messages):
                    return ModelDecision("final", "事实 [S1] 和伪造事实 [S999] [S" + "9" * 5000 + "]")
                return ModelDecision("tool_call", query="learn-claude-code", call_id="c1", finish_reason="tool_calls")

        with tempfile.TemporaryDirectory() as tmp:
            search = FixtureSearch.from_file(ROOT / "fixtures" / "search_results.json")
            result = ResearchAgent(search, MaliciousModel(), tmp).run("请查明 learn-claude-code")
            self.assertEqual(result.status, "insufficient")
            self.assertIn("[S1]", result.answer)
            self.assertIn("[UNVERIFIED_CITATION]", result.answer)
            self.assertNotIn("[S999]", result.answer)
            self.assertEqual(len(result.invalid_citations), 2)

    def test_invalid_url_is_discarded(self):
        class OneShotModel:
            name = "one-shot"

            def complete(self, messages, tools):
                if any(message.get("role") == "tool" for message in messages):
                    return ModelDecision("final", "有来源 [S1]")
                return ModelDecision("tool_call", query="test", call_id="c1", finish_reason="tool_calls")

        class BadURLSearch:
            def search(self, query):
                return SearchResponse(True, query, [
                    {"title": "bad", "url": "https://", "snippet": "not a source"},
                    {"title": "credential query", "url": "https://example.com/a?api_key=secret", "snippet": "not a source"},
                    {"title": "space", "url": "https://example.com/a b", "snippet": "not a source"},
                    {"title": "good", "url": "https://example.com/a", "snippet": "real source"},
                ])

        with tempfile.TemporaryDirectory() as tmp:
            result = ResearchAgent(BadURLSearch(), OneShotModel(), tmp).run("请查明 test")
            self.assertEqual([source.url for source in result.sources], ["https://example.com/a"])
            self.assertEqual(result.status, "ok")

    def test_trace_redacts_credentials(self):
        class ErrorModel:
            name = "error-model"

            def complete(self, messages, tools):
                raise RuntimeError("Bearer super-secret-token sk-12345678901234567890")

        with tempfile.TemporaryDirectory() as tmp:
            result = ResearchAgent(FailingSearch("api_key=private-value"), ErrorModel(), tmp).run("请查明事实")
            trace_text = Path(result.trace_path).read_text(encoding="utf-8")
            self.assertNotIn("super-secret-token", trace_text)
            self.assertNotIn("12345678901234567890", trace_text)
            self.assertNotIn("private-value", trace_text)

    def test_camel_case_credentials_are_redacted(self):
        secrets = [
            "access-secret", "refresh-secret", "client-secret", "key-secret", "auth-secret", "proxy-secret",
            "nested-access", "nested-client", "nested-key", "nested-password", "TAIL_SECRET",
        ]
        value = redact({
            "accessToken": secrets[0],
            "message": f"refreshToken={secrets[1]} clientSecret={secrets[2]}",
            "key": secrets[3],
            "raw": f"authorization={secrets[4]}&key = {secrets[3]} accessToken: \"{secrets[0]} with spaces\" "
                   f"{{\"clientSecret\":\"{secrets[2]}&suffix\"}} CLIENTSECRET='{secrets[2]} with spaces' "
                   "SK-1234567890123456",
            "ACCESSTOKEN": secrets[0],
            "proxyAuthorization": secrets[5],
            "promptTokens": 12,
            "nested": (
                'Provider response: {"accessToken":"nested-access"} '
                'error={"clientSecret":"nested-client"} payload: accessToken=nested-access '
                'headers: {"X-Api-Key":"nested-key"}'
            ),
            "escaped_inner_quote": r'{"password":"alpha\"TAIL_SECRET"}',
            "escaped_outer_inner_quote": r'{\"password\":\"alpha\\\"TAIL_SECRET\"}',
        })
        serialized = json.dumps(value)
        self.assertTrue(all(secret not in serialized for secret in secrets))
        self.assertNotIn("1234567890123456", serialized)
        self.assertEqual(value["promptTokens"], 12)
        self.assertEqual(redact(value), value)
        self.assertEqual(redact(r'{"password":"alpha\"TAIL_SECRET"}'), r'{"password":"[REDACTED]"}')
        self.assertEqual(redact(r'{\"password\":\"alpha\\\"TAIL_SECRET\"}'), r'{\"password\":\"[REDACTED]\"}')
        for opener in ('"', "'", r'\"', r"\'"):
            malformed = f"password={opener}UNTERMINATED_SECRET"
            self.assertEqual(redact(malformed), f"password={opener}[REDACTED]")
        self.assertEqual(
            redact('{"message":"authorization: AUTH_SECRET"}'),
            '{"message":"authorization: [REDACTED]"}',
        )
        for header, expected in (
            ("Authorization: Bearer abcdefghijklmnop", "Authorization: [REDACTED]"),
            ("Authorization: Basic YWxwaGE6YmV0YQ==", "Authorization: [REDACTED]"),
            ("Authorization: [REDACTED]", "Authorization: [REDACTED]"),
            ('{"message":"Authorization: AUTH_SECRET"}', '{"message":"Authorization: [REDACTED]"}'),
        ):
            once = redact(header)
            self.assertEqual(once, expected)
            self.assertEqual(redact(once), once)
        ordinary = "Press key=Enter. The key: finding. MONKEY=value."
        self.assertEqual(redact(ordinary), ordinary)
        self.assertEqual(redact({"MONKEY": "value"}), {"MONKEY": "value"})

    def test_credential_redaction_avoids_quadratic_separator_scan(self):
        def elapsed(repetitions):
            value = "a_" * repetitions + "x"
            samples = []
            for _ in range(3):
                started = time.perf_counter()
                redact(value)
                samples.append(time.perf_counter() - started)
            return min(samples)

        redact("warmup")
        small = elapsed(8000)
        large = elapsed(16000)
        self.assertLess(large, 0.5)
        self.assertLess(large / small, 3.0)

    def test_no_tool_search_claim_is_rejected(self):
        class HallucinatingModel:
            name = "hallucinating-fixture"

            def complete(self, messages, tools):
                return ModelDecision("final", "我已经搜索了官方文档，结果显示该项目可靠。")

        with tempfile.TemporaryDirectory() as tmp:
            result = ResearchAgent(FailingSearch(), HallucinatingModel(), tmp).run("你好")
            self.assertEqual(result.status, "insufficient")
            self.assertIn("没有真实工具结果", result.answer)

    def test_no_citation_after_success_is_insufficient(self):
        class UncitedModel:
            name = "uncited-fixture"

            def complete(self, messages, tools):
                if any(message.get("role") == "tool" for message in messages):
                    return ModelDecision("final", "这是一个无引用的确定事实。")
                return ModelDecision("tool_call", query="learn-claude-code", call_id="c1", finish_reason="tool_calls")

        with tempfile.TemporaryDirectory() as tmp:
            result = ResearchAgent(FixtureSearch.from_file(ROOT / "fixtures" / "search_results.json"), UncitedModel(), tmp).run("请查明 learn-claude-code")
            self.assertEqual(result.status, "insufficient")
            self.assertIn("没有把事实绑定到有效引用", result.answer)

    def test_model_failure_after_search_preserves_redacted_cause(self):
        class FailedModel:
            name = "failed-after-search"

            def complete(self, messages, tools):
                if any(message.get("role") == "tool" for message in messages):
                    raise RuntimeError("模型服务响应超时（等待上限 180 秒） api_key=private-value")
                return ModelDecision("tool_call", query="learn-claude-code", call_id="c1")

        with tempfile.TemporaryDirectory() as tmp:
            result = ResearchAgent(FixtureSearch.from_file(ROOT / "fixtures/search_results.json"), FailedModel(), tmp).run("请查明 learn-claude-code")
            self.assertEqual(result.status, "insufficient")
            self.assertEqual(result.termination, "model_error")
            self.assertTrue(result.sources)
            self.assertIn("响应超时", result.answer)
            self.assertNotIn("没有把事实绑定", result.answer)
            self.assertNotIn("private-value", result.answer)

    def test_duplicate_action_reuses_evidence_then_allows_one_final_answer(self):
        test = self

        class RepeatThenFinish:
            name = "repeat-then-finish"

            def complete(self, messages, tools):
                if not tools:
                    payload = json.loads(next(m["content"] for m in reversed(messages) if m["role"]=="tool"))["UNTRUSTED_TOOL_DATA"]
                    test.assertTrue(payload["cached"])
                    test.assertEqual(payload["network_requests"], 0)
                    test.assertEqual(payload["results"][0]["source_id"], "S1")
                    return ModelDecision("final", "This tutorial builds an agent incrementally. [S1] [E1]")
                return ModelDecision("tool_call", query="learn-claude-code", call_id="repeat")

        with tempfile.TemporaryDirectory() as tmp:
            search = FixtureSearch.from_file(ROOT / "fixtures/search_results.json")
            with patch.object(search, "search", wraps=search.search) as calls:
                result = ResearchAgent(search, RepeatThenFinish(), tmp, max_tool_calls=3).run("请查明 learn-claude-code")
            self.assertEqual(calls.call_count, 1)
            self.assertEqual(result.status, "ok")
            self.assertEqual(result.termination, "model_final")
            self.assertEqual(result.tool_calls, 1)
            self.assertEqual(result.valid_citations, ["S1"])

    def test_malformed_search_response_is_safe_failure(self):
        class SearchModel:
            name = "search-response-fixture"

            def complete(self, messages, tools):
                if any(message.get("role") == "tool" for message in messages):
                    return ModelDecision("final", "INSUFFICIENT：工具失败。")
                return ModelDecision("tool_call", query="test", call_id="c1", finish_reason="tool_calls")

        class MalformedSearch:
            def search(self, query):
                return SearchResponse(True, query, None)

        with tempfile.TemporaryDirectory() as tmp:
            result = ResearchAgent(MalformedSearch(), SearchModel(), tmp).run("请查明 test")
            self.assertEqual(result.status, "insufficient")
            self.assertEqual(result.sources, [])
            trace = Path(result.trace_path).read_text(encoding="utf-8")
            self.assertIn("invalid_search_response", trace)

    def test_credential_like_url_and_basic_error_are_redacted(self):
        class SearchModel:
            name = "credential-fixture"

            def complete(self, messages, tools):
                if any(message.get("role") == "tool" for message in messages):
                    raise RuntimeError("Authorization: Basic ZHVtbXktc2VjcmV0")
                return ModelDecision("tool_call", query="test", call_id="c1", finish_reason="tool_calls")

        class CredentialSearch:
            def search(self, query):
                return SearchResponse(True, query, [
                    {"title": "private", "url": "https://user:real-secret-123@example.com/x", "snippet": "private"},
                ])

        with tempfile.TemporaryDirectory() as tmp:
            result = ResearchAgent(CredentialSearch(), SearchModel(), tmp).run("请查明 test")
            trace = Path(result.trace_path).read_text(encoding="utf-8")
            self.assertNotIn("real-secret-123", trace)
            self.assertNotIn("ZHVtbXktc2VjcmV0", trace)
            self.assertNotIn("real-secret-123", result.answer)
            self.assertEqual(result.sources, [])

    def test_english_unsupported_search_claim_is_rejected(self):
        class EnglishHallucination:
            name = "english-hallucination"

            def complete(self, messages, tools):
                return ModelDecision("final", "I searched the sources and found the answer.")

        with tempfile.TemporaryDirectory() as tmp:
            result = ResearchAgent(FailingSearch(), EnglishHallucination(), tmp).run("hello")
            self.assertEqual(result.status, "insufficient")
            self.assertIn("没有真实工具结果", result.answer)

    def test_non_json_search_metadata_cannot_crash_trace(self):
        class SearchModel:
            name = "non-json-fixture"

            def complete(self, messages, tools):
                if any(message.get("role") == "tool" for message in messages):
                    return ModelDecision("final", "事实 [S1]")
                return ModelDecision("tool_call", query="test", call_id="c1", finish_reason="tool_calls")

        class OddObject:
            def __str__(self):
                return "odd-object"

        class NonJsonSearch:
            def search(self, query):
                return SearchResponse(True, query, [
                    {"title": "odd", "url": "https://example.com/odd", "snippet": "ok", "publisher": OddObject(), "published_at": OddObject()},
                ])

        with tempfile.TemporaryDirectory() as tmp:
            result = ResearchAgent(NonJsonSearch(), SearchModel(), tmp).run("请查明 test")
            self.assertEqual(result.status, "ok")
            trace = Path(result.trace_path).read_text(encoding="utf-8")
            self.assertIn("odd-object", trace)


if __name__ == "__main__":
    unittest.main()
