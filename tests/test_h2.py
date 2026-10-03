import json
import gzip
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import research_agent.search as search_module
from research_agent import ModelDecision, OpenAICompatibleModel, ReadResponse, ResearchAgent, RunStore, SearchResponse
from research_agent.policy import before_finalize, before_tool


PUBLIC_ADDRESSES = [
    (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 443)),
]


class SequenceModel:
    name = "h2-sequence-model"

    def __init__(self, *decisions):
        self.decisions = list(decisions)
        self.tools_seen = []

    def complete(self, messages, tools):
        self.tools_seen.append(tools)
        if not self.decisions:
            raise AssertionError("model received an unexpected extra call")
        return self.decisions.pop(0)


class ScriptedSearch:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = 0

    def search(self, query):
        self.calls += 1
        if not self.responses:
            raise AssertionError("search received an unexpected extra call")
        return self.responses.pop(0)


class ScriptedReader:
    def __init__(self, response):
        self.response = response
        self.calls = 0

    def read(self, url):
        self.calls += 1
        return self.response


def source_response(query="topic", network_requests=1):
    return SearchResponse(
        True,
        query,
        [{"title": "Source", "url": "https://example.com/source", "snippet": "fact"}],
        network_requests=network_requests,
    )


class URLPolicyTests(unittest.TestCase):
    def test_canonical_url_accepts_only_narrow_equivalences(self):
        self.assertEqual(
            search_module.canonical_url("HTTPS://Example.COM.:443/a?b=1#section"),
            "https://example.com/a?b=1",
        )
        self.assertEqual(search_module.canonical_url("http://example.com"), "http://example.com/")
        self.assertEqual(search_module.canonical_url("https://example.com:8443/a"), "https://example.com:8443/a")

    def test_private_local_reserved_and_multicast_literals_are_rejected(self):
        blocked = [
            "http://localhost./x",
            "http://127.0.0.1/x",
            "http://127.1/x",
            "http://2130706433/x",
            "http://0177.0.0.1/x",
            "http://[::1]/x",
            "http://[::ffff:127.0.0.1]/x",
            "http://100.64.0.1/x",
            "http://192.0.2.1/x",
            "http://224.0.0.1/x",
        ]
        for url in blocked:
            with self.subTest(url=url):
                self.assertIsNone(search_module.canonical_url(url))

    def test_mixed_public_and_private_dns_answers_are_rejected(self):
        resolver = Mock(return_value=[
            *PUBLIC_ADDRESSES,
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("10.0.0.1", 443)),
        ])

        with self.assertRaises(search_module.UnsafeNetworkTarget):
            search_module.resolve_public_addresses("example.com", 443, resolver=resolver)
        resolver.assert_called_once_with("example.com", 443, type=socket.SOCK_STREAM)

    def test_pinned_connection_uses_checked_address_and_rechecks_peer(self):
        connected = Mock()
        connected.getpeername.return_value = ("93.184.216.34", 443)
        with patch.object(search_module.socket, "socket", return_value=connected):
            result = search_module._connect_pinned(PUBLIC_ADDRESSES, 2.0)
        self.assertIs(result, connected)
        connected.connect.assert_called_once_with(("93.184.216.34", 443))

        rebound = Mock()
        rebound.getpeername.return_value = ("127.0.0.1", 443)
        with patch.object(search_module.socket, "socket", return_value=rebound):
            with self.assertRaises(search_module.UnsafeNetworkTarget):
                search_module._connect_pinned(PUBLIC_ADDRESSES, 2.0)
        rebound.close.assert_called_once()

    def test_credential_url_is_denied_without_retaining_secret(self):
        for label, url, secret in (
            ("userinfo", "https://user:super-secret-password@example.com/private", "super-secret-password"),
            ("api-key", "https://example.com/private?api_key=super-secret-token", "super-secret-token"),
            ("access-token", "https://example.com/private?access_token=plain-access-secret", "plain-access-secret"),
            ("encoded-key", "https://example.com/private?access%5Ftoken=encoded-access-secret", "encoded-access-secret"),
        ):
            with self.subTest(case=label):
                audit = before_tool("read", url=url, allowed_urls={url})
                self.assertEqual((audit.decision, audit.reason), ("deny", "credential_url"))
                self.assertFalse(secret in audit.target)
        self.assertEqual(
            search_module.canonical_url("https://example.com/?MONKEY=value"),
            "https://example.com/?MONKEY=value",
        )
        self.assertIsNone(search_module.canonical_url("https://example.com/?key=secret"))

    def test_percent_encoded_credential_never_reaches_trace_result_or_sqlite(self):
        secret = "encoded-access-secret"
        url = f"https://example.com/private?access%5Ftoken={secret}"
        model = SequenceModel(ModelDecision("tool_call", tool_name="read", url=url, call_id="credential-read"))

        with tempfile.TemporaryDirectory() as tmp:
            result = ResearchAgent(ScriptedSearch(), model, tmp, reader=ScriptedReader(
                ReadResponse(True, url, "must not run", network_requests=1),
            )).run("question?")
            trace = Path(result.trace_path).read_text(encoding="utf-8")
            store = RunStore(Path(tmp) / "runs.db")
            run_id = store.save(result, "question?", "2026-09-14T00:00:00+00:00")
            stored = json.dumps(store.get(run_id), ensure_ascii=False)

        self.assertEqual(result.termination, "policy_denied")
        self.assertFalse(secret in trace)
        self.assertFalse(any(secret in event.target for event in result.audit_events))
        self.assertFalse(secret in stored)

    def test_authorization_accepts_canonical_equivalent_but_not_same_domain_sibling(self):
        allowed = {"HTTPS://Example.COM:443/source#result"}
        self.assertEqual(before_tool("read", url="https://example.com/source", allowed_urls=allowed).decision, "allow")
        self.assertEqual(before_tool("read", url="https://example.com/other", allowed_urls=allowed).decision, "deny")


class HttpReaderTests(unittest.TestCase):
    def run_reader(self, responses, url="https://example.com/a/start", max_redirects=3):
        with (
            patch.object(search_module, "resolve_public_addresses", return_value=PUBLIC_ADDRESSES) as resolve,
            patch.object(search_module, "_request_once", side_effect=responses) as request,
        ):
            result = search_module.HttpReader(max_redirects=max_redirects).read(url)
        return result, resolve, request

    def test_relative_redirect_is_resolved_and_every_hop_is_checked(self):
        result, resolve, request = self.run_reader([
            search_module._NetworkResponse(302, {"location": "../next"}, b""),
            search_module._NetworkResponse(200, {"content-type": "text/plain; charset=utf-8"}, b"done"),
        ])

        self.assertTrue(result.ok)
        self.assertEqual(result.content, "done")
        self.assertEqual(result.final_url, "https://example.com/next")
        self.assertEqual(result.network_requests, 2)
        self.assertEqual(resolve.call_count, 2)
        self.assertEqual([call.args[0] for call in request.call_args_list], [
            "https://example.com/a/start",
            "https://example.com/next",
        ])

    def test_private_literal_returns_without_resolving_or_requesting(self):
        with (
            patch.object(search_module, "resolve_public_addresses") as resolve,
            patch.object(search_module, "_request_once") as request,
        ):
            result = search_module.HttpReader().read("http://127.0.0.1/private")

        self.assertFalse(result.ok)
        self.assertEqual(result.error["code"], "network_target_denied")
        self.assertEqual(result.network_requests, 0)
        resolve.assert_not_called()
        request.assert_not_called()

    def test_redirect_to_private_target_is_blocked_before_second_request(self):
        result, resolve, request = self.run_reader([
            search_module._NetworkResponse(302, {"location": "http://127.0.0.1/private"}, b""),
        ])

        self.assertFalse(result.ok)
        self.assertEqual(result.error["code"], "unsafe_redirect")
        self.assertEqual(result.network_requests, 1)
        self.assertEqual(resolve.call_count, 1)
        self.assertEqual(request.call_count, 1)

    def test_redirect_dns_is_rechecked_and_private_answer_is_denied(self):
        with (
            patch.object(
                search_module,
                "resolve_public_addresses",
                side_effect=[PUBLIC_ADDRESSES, search_module.UnsafeNetworkTarget("private redirect")],
            ) as resolve,
            patch.object(
                search_module,
                "_request_once",
                return_value=search_module._NetworkResponse(
                    302,
                    {"location": "https://redirect.example/private"},
                    b"",
                ),
            ) as request,
        ):
            result = search_module.HttpReader().read("https://example.com/start")

        self.assertFalse(result.ok)
        self.assertEqual(result.error["code"], "network_target_denied")
        self.assertEqual(result.network_requests, 1)
        self.assertEqual(resolve.call_count, 2)
        self.assertEqual(request.call_count, 1)

    def test_redirect_limit_is_bounded(self):
        result, resolve, request = self.run_reader([
            search_module._NetworkResponse(302, {"location": "/one"}, b""),
            search_module._NetworkResponse(302, {"location": "/two"}, b""),
        ], max_redirects=1)

        self.assertFalse(result.ok)
        self.assertEqual(result.error["code"], "too_many_redirects")
        self.assertEqual(result.network_requests, 2)
        self.assertEqual(resolve.call_count, 2)
        self.assertEqual(request.call_count, 2)

    def test_html_and_plain_text_are_supported(self):
        for content_type, body in (
            ("text/html; charset=utf-8", b"<p>ok</p>"),
            ("text/plain", b"plain text"),
        ):
            with self.subTest(content_type=content_type):
                result, _, _ = self.run_reader([
                    search_module._NetworkResponse(200, {"content-type": content_type}, body),
                ])
                self.assertTrue(result.ok)
                self.assertEqual(result.content, "ok" if content_type.startswith("text/html") else body.decode())
                self.assertEqual(result.network_requests, 1)

    def test_gzip_publisher_pages_decode_with_or_without_encoding_header(self):
        for declared in (False, True):
            headers = {"content-type":"text/html; charset=utf-8"}
            if declared: headers["content-encoding"] = "gzip"
            result, _, _ = self.run_reader([search_module._NetworkResponse(200, headers, gzip.compress(
                '<html><title>论文</title><body><p>经验用于后续决策。</p></body></html>'.encode()))])
            self.assertTrue(result.ok)
            self.assertEqual(result.title, '论文')
            self.assertIn('经验用于后续决策。', result.content)
            self.assertNotIn('\ufffd', result.content)

    def test_compressed_output_is_bounded_and_corrupt_input_is_not_evidence(self):
        headers = {"content-type":"text/plain", "content-encoding":"gzip"}
        response = search_module._NetworkResponse(200, headers, gzip.compress(b'a'*1_000_000))
        with patch.object(search_module, 'resolve_public_addresses', return_value=PUBLIC_ADDRESSES), patch.object(
            search_module, '_request_once', return_value=response
        ):
            result = search_module.HttpReader(max_bytes=100).read('https://example.com/paper')
        self.assertTrue(result.ok)
        self.assertTrue(result.truncated)
        self.assertLessEqual(len(result.content),100)
        for body in (b'broken gzip', gzip.compress(b'paper')[:-5]):
            result, _, _ = self.run_reader([search_module._NetworkResponse(200, headers, body)])
            self.assertFalse(result.ok)
            self.assertEqual(result.error['code'],'invalid_text_encoding')

    def test_html_keeps_tables_plain_divs_and_nested_list_tails(self):
        cases = (
            (
                b"<table><tr><th>Metric</th><th>Value</th></tr><tr><td>Accuracy</td><td>97%</td></tr></table>",
                "Metric\n\nValue\n\nAccuracy\n\n97%",
            ),
            (b"<div>Plain <strong>evidence</strong> is 42.</div>", "Plain evidence is 42."),
            (
                b"<ul><li>outer start<ul><li>inner fact</li></ul>outer tail</li></ul>",
                "- outer start inner fact outer tail",
            ),
            (b"<p>control</p><p>control</p>", "control\n\ncontrol"),
            (b"<div hidden>hidden<br>still hidden</div><template>template</template><p>visible</p>", "visible"),
            (
                b"<section>A</section><section>B</section><blockquote>C</blockquote><dt>D</dt><dd>E</dd>",
                "A\n\nB\n\nC\n\nD\n\nE",
            ),
            (
                b"<nav><ul><li>A<li>B</ul></nav><main>VISIBLE_BODY</main>",
                "VISIBLE_BODY",
            ),
            (
                b"<header>PAGE</header><main><header><h1>MAIN</h1><p>LEAD</p></header><p>BODY</p></main>",
                "# MAIN\n\nLEAD\n\nBODY",
            ),
            (
                b"<article><header><h1>ARTICLE</h1><nav><header>NAV_NOISE</header></nav><p>LEAD</p></header><p>BODY</p></article>",
                "# ARTICLE\n\nLEAD\n\nBODY",
            ),
        )
        for body, expected in cases:
            with self.subTest(body=body):
                result, _, _ = self.run_reader([
                    search_module._NetworkResponse(200, {"content-type": "text/html"}, body),
                ])
                self.assertTrue(result.ok)
                self.assertEqual(result.content, expected)

    def test_binary_json_and_missing_media_types_are_rejected(self):
        for content_type in ("application/pdf", "application/json", None):
            with self.subTest(content_type=content_type):
                headers = {} if content_type is None else {"content-type": content_type}
                result, _, _ = self.run_reader([
                    search_module._NetworkResponse(200, headers, b"must not become evidence"),
                ])
                self.assertFalse(result.ok)
                self.assertEqual(result.error["code"], "unsupported_media_type")
                self.assertEqual(result.content, "")
                self.assertEqual(result.network_requests, 1)


class LoopRecoveryTests(unittest.TestCase):
    def test_unlisted_public_page_is_not_read_and_model_can_correct_via_search(self):
        url = 'https://example.com/source'
        model = SequenceModel(ModelDecision('tool_call', tool_name='read', url=url, call_id='unlisted'),
            ModelDecision('tool_call', tool_name='search', query=url, call_id='lookup'),
            ModelDecision('tool_call', tool_name='read', url=url, call_id='authorized'),
            ModelDecision('final', content='A supported fact. [E2]'))
        search = ScriptedSearch(source_response())
        reader = ScriptedReader(ReadResponse(True, url, 'A supported fact.', network_requests=1))
        with tempfile.TemporaryDirectory() as tmp:
            result = ResearchAgent(search, model, tmp, reader=reader, max_tool_calls=4, max_rounds=6).run('What is the fact?')
        self.assertEqual(reader.calls, 1)
        self.assertEqual(search.calls, 1)
        self.assertEqual(result.tool_denials, 1)
        self.assertEqual(result.status, 'ok')

    def test_unknown_tool_is_hard_denied_without_execution(self):
        model = SequenceModel(ModelDecision("tool_call", tool_name="shell", call_id="unknown"))
        search = ScriptedSearch()

        with tempfile.TemporaryDirectory() as tmp:
            result = ResearchAgent(search, model, tmp).run("question?")

        self.assertEqual(result.termination, "policy_denied")
        self.assertEqual(search.calls, 0)
        self.assertEqual(
            (result.tool_attempts, result.network_requests, result.tool_denials, result.tool_successes),
            (1, 0, 1, 0),
        )

    def test_two_parameter_corrections_can_recover(self):
        model = SequenceModel(
            ModelDecision("tool_call", query="", call_id="bad-1"),
            ModelDecision("tool_call", query="x" * 301, call_id="bad-2"),
            ModelDecision("tool_call", query="topic", call_id="good"),
            ModelDecision("final", "fact [S1]"),
        )
        search = ScriptedSearch(source_response())

        with tempfile.TemporaryDirectory() as tmp:
            result = ResearchAgent(search, model, tmp, max_tool_calls=3, max_rounds=4).run("question?")

        self.assertEqual(result.status, "ok")
        self.assertEqual(search.calls, 1)
        self.assertEqual(
            (result.tool_attempts, result.network_requests, result.tool_denials, result.tool_successes),
            (3, 1, 0, 1),
        )

    def test_third_parameter_error_stops_without_executing_tool(self):
        model = SequenceModel(*[
            ModelDecision("tool_call", query="", call_id=f"bad-{index}")
            for index in range(3)
        ])
        search = ScriptedSearch()

        with tempfile.TemporaryDirectory() as tmp:
            result = ResearchAgent(search, model, tmp, max_tool_calls=3, max_rounds=4).run("question?")

        self.assertEqual(result.termination, "invalid_tool_arguments")
        self.assertEqual(search.calls, 0)
        self.assertEqual(result.tool_attempts, 3)
        self.assertEqual(result.network_requests, 0)
        self.assertEqual(result.tool_successes, 0)

    def test_transient_network_failure_is_retried_once_and_counted(self):
        search = ScriptedSearch(
            SearchResponse(False, "topic", error={"code": "search_transient", "message": "timeout"}, network_requests=1),
            source_response(),
        )
        model = SequenceModel(
            ModelDecision("tool_call", query="topic", call_id="search"),
            ModelDecision("final", "fact [S1]"),
        )

        with tempfile.TemporaryDirectory() as tmp:
            result = ResearchAgent(search, model, tmp, max_tool_calls=2).run("question?")

        self.assertEqual(search.calls, 2)
        self.assertEqual(
            (result.tool_attempts, result.network_requests, result.tool_denials, result.tool_successes),
            (1, 2, 0, 1),
        )

    def test_transient_network_failure_never_exceeds_one_retry(self):
        failure = lambda: SearchResponse(
            False,
            "topic",
            error={"code": "search_transient", "message": "timeout"},
            network_requests=1,
        )
        search = ScriptedSearch(failure(), failure())
        model = SequenceModel(
            ModelDecision("tool_call", query="topic", call_id="search"),
            ModelDecision("final", "INSUFFICIENT: search failed"),
        )

        with tempfile.TemporaryDirectory() as tmp:
            result = ResearchAgent(search, model, tmp, max_tool_calls=2).run("question?")

        self.assertEqual(search.calls, 2)
        self.assertEqual(
            (result.tool_attempts, result.network_requests, result.tool_denials, result.tool_successes),
            (1, 2, 0, 0),
        )

    def test_non_transient_failure_is_not_retried(self):
        search = ScriptedSearch(SearchResponse(
            False,
            "topic",
            error={"code": "invalid_request", "message": "bad request"},
            network_requests=1,
        ))
        model = SequenceModel(
            ModelDecision("tool_call", query="topic", call_id="search"),
            ModelDecision("final", "INSUFFICIENT: invalid request"),
        )

        with tempfile.TemporaryDirectory() as tmp:
            result = ResearchAgent(search, model, tmp, max_tool_calls=2).run("question?")

        self.assertEqual(search.calls, 1)
        self.assertEqual(
            (result.tool_attempts, result.network_requests, result.tool_denials, result.tool_successes),
            (1, 1, 0, 0),
        )

    def test_budget_exhaustion_gets_one_final_only_summary_call(self):
        model = SequenceModel(
            ModelDecision("tool_call", query="topic", call_id="search"),
            ModelDecision("final", "summary from existing evidence [S1]"),
        )
        search = ScriptedSearch(source_response())

        with tempfile.TemporaryDirectory() as tmp:
            result = ResearchAgent(search, model, tmp, max_tool_calls=1).run("question?")

        self.assertEqual(result.status, "ok")
        self.assertEqual(len(model.tools_seen), 2)
        self.assertTrue(model.tools_seen[0])
        self.assertEqual(model.tools_seen[1], [])
        self.assertEqual(search.calls, 1)

    def test_tool_requested_during_final_only_is_not_executed(self):
        model = SequenceModel(
            ModelDecision("tool_call", query="topic", call_id="search"),
            ModelDecision("tool_call", query="must-not-run", call_id="forbidden"),
        )
        search = ScriptedSearch(source_response())

        with tempfile.TemporaryDirectory() as tmp:
            result = ResearchAgent(search, model, tmp, max_tool_calls=1).run("question?")

        self.assertEqual(result.termination, "budget_exhausted")
        self.assertEqual(model.tools_seen[-1], [])
        self.assertEqual(search.calls, 1)
        self.assertEqual(
            (result.tool_attempts, result.network_requests, result.tool_denials, result.tool_successes),
            (2, 1, 1, 1),
        )

    def test_hard_denial_has_no_network_or_success_count_and_leaks_no_secret(self):
        secret = "super-secret-password"
        model = SequenceModel(ModelDecision(
            "tool_call",
            tool_name="read",
            url=f"https://user:{secret}@example.com/private",
            call_id="credential-read",
        ))
        reader = ScriptedReader(ReadResponse(True, "https://example.com/private", "must not run", network_requests=1))

        with tempfile.TemporaryDirectory() as tmp:
            result = ResearchAgent(ScriptedSearch(), model, tmp, reader=reader).run("question?")
            trace = Path(result.trace_path).read_text(encoding="utf-8")

        self.assertEqual(result.termination, "policy_denied")
        self.assertEqual(reader.calls, 0)
        self.assertEqual(
            (result.tool_attempts, result.network_requests, result.tool_denials, result.tool_successes),
            (1, 0, 1, 0),
        )
        self.assertNotIn(secret, trace)
        self.assertTrue(all(secret not in event.target for event in result.audit_events))

    def test_redirect_requests_do_not_inflate_tool_attempt_or_success_counts(self):
        model = SequenceModel(
            ModelDecision("tool_call", query="topic", call_id="search"),
            ModelDecision("tool_call", tool_name="read", url="https://example.com/source", call_id="read"),
            ModelDecision("final", "fact [S1] [E2]"),
        )
        search = ScriptedSearch(source_response(network_requests=0))
        reader = ScriptedReader(ReadResponse(
            True,
            "https://example.com/source",
            "fact",
            network_requests=2,
            media_type="text/plain",
            final_url="https://www.example.com/final",
        ))

        with tempfile.TemporaryDirectory() as tmp:
            result = ResearchAgent(search, model, tmp, max_tool_calls=2, reader=reader).run("question?")

        self.assertEqual(result.status, "ok")
        self.assertEqual(
            (result.tool_attempts, result.network_requests, result.tool_denials, result.tool_successes),
            (2, 2, 0, 2),
        )

    def test_network_target_denial_is_counted_without_an_http_request(self):
        model = SequenceModel(
            ModelDecision("tool_call", query="topic", call_id="search"),
            ModelDecision("tool_call", tool_name="read", url="https://example.com/source", call_id="read"),
            ModelDecision("final", "INSUFFICIENT: unsafe network target"),
        )
        search = ScriptedSearch(source_response())
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(
                search_module,
                "resolve_public_addresses",
                side_effect=search_module.UnsafeNetworkTarget("private DNS answer"),
            ),
        ):
            result = ResearchAgent(
                search,
                model,
                tmp,
                max_tool_calls=2,
                reader=search_module.HttpReader(),
            ).run("question?")

        self.assertEqual(result.termination, "model_final")
        self.assertEqual(
            (result.tool_attempts, result.network_requests, result.tool_denials, result.tool_successes),
            (2, 1, 1, 1),
        )


class ModelAdapterTests(unittest.TestCase):
    def test_empty_tools_are_omitted_from_openai_request(self):
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return json.dumps({
                    "choices": [{"message": {"content": "done"}, "finish_reason": "stop"}],
                }).encode()

        with patch("research_agent.models.urlopen", return_value=FakeResponse()) as urlopen:
            decision = OpenAICompatibleModel("https://api.example/v1", "secret", "model").complete(
                [{"role": "user", "content": "question"}],
                [],
            )

        payload = json.loads(urlopen.call_args.args[0].data)
        self.assertEqual(decision.kind, "final")
        self.assertNotIn("tools", payload)
        self.assertNotIn("tool_choice", payload)

    def test_malformed_tool_arguments_can_be_corrected(self):
        class FakeResponse:
            def __init__(self, payload):
                self.payload = payload

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return json.dumps(self.payload).encode()

        responses = [
            FakeResponse({
                "choices": [{
                    "message": {"tool_calls": [{
                        "id": "bad",
                        "function": {"name": "search", "arguments": "{"},
                    }]},
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
            FakeResponse({
                "choices": [{"message": {"content": "fact [S1]"}, "finish_reason": "stop"}],
            }),
        ]
        model = OpenAICompatibleModel("https://api.example/v1", "secret", "model")
        search = ScriptedSearch(source_response())

        with tempfile.TemporaryDirectory() as tmp, patch("research_agent.models.urlopen", side_effect=responses) as urlopen:
            # Isolate the adapter's malformed-argument recovery from semantic finalization.
            result = ResearchAgent(search, model, tmp, max_tool_calls=2, answer_verification=False).run("question?")

        self.assertEqual(urlopen.call_count, 3)
        self.assertEqual(search.calls, 1)
        self.assertEqual((result.status, result.termination), ("ok", "model_final"))
        self.assertEqual(
            (result.tool_attempts, result.network_requests, result.tool_denials, result.tool_successes),
            (2, 1, 0, 1),
        )


class OutputPolicyTests(unittest.TestCase):
    def test_system_prompt_technical_explanation_is_not_treated_as_a_dump(self):
        audit = before_finalize("System prompt: a high-priority instruction supplied to a language model.")
        self.assertEqual((audit.decision, audit.reason), ("allow", ""))


if __name__ == "__main__":
    unittest.main()
