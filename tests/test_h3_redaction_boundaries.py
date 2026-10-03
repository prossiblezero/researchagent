import json
import tempfile
import unittest
from pathlib import Path

from research_agent import FailingSearch, ModelDecision, ResearchAgent
from research_agent.trace import redact


class H3RedactionBoundaryTests(unittest.TestCase):
    def test_full_receipts_preserve_json_boundaries_and_redact_credentials(self):
        source = json.dumps({'body': 'x' * 17000, 'nested': {'password': 'PRIVATE_TAIL'}})
        value = {'stdout': source + '\n' + json.dumps({'type': 'turn.completed'}),
                 'state': json.dumps({'content': 'y' * 18000, 'api_key': 'PRIVATE_KEY'})}
        cleaned = redact(value, limit=None)
        self.assertEqual(len(json.loads(cleaned['stdout'].splitlines()[0])['body']), 17000)
        self.assertEqual(json.loads(cleaned['stdout'].splitlines()[1])['type'], 'turn.completed')
        self.assertEqual(len(json.loads(cleaned['state'])['content']), 18000)
        self.assertNotIn('PRIVATE_TAIL', json.dumps(cleaned))
        self.assertNotIn('PRIVATE_KEY', json.dumps(cleaned))
        self.assertEqual(len(redact('z' * 18000)), 12000)

    def test_marker_prefixed_assignments_redact_the_entire_value(self):
        for source, expected in (
            ("password=[REDACTED]后续秘密", "password=[REDACTED]"),
            ("clientSecret=[REDACTED]TAIL_SECRET", "clientSecret=[REDACTED]"),
            ("password=[REDACTED][REDACTED]TAIL_SECRET", "password=[REDACTED]"),
            ("password=[REDACTED][REDACTED]", "password=[REDACTED]"),
            ("?password=[REDACTED]TAIL_SECRET&page=2", "?password=[REDACTED]&page=2"),
            ('password="[REDACTED]TAIL_SECRET with spaces"', 'password="[REDACTED]"'),
            ('{"message":"password=[REDACTED]TAIL_SECRET"}', '{"message":"password=[REDACTED]"}'),
            ('{"message":"password=[REDACTED]"}', '{"message":"password=[REDACTED]"}'),
            (r'{\"message\":\"password=[REDACTED]\"}', r'{\"message\":\"password=[REDACTED]\"}'),
            ('["password=[REDACTED]"]', '["password=[REDACTED]"]'),
            ("password=[REDACTED] prompt_tokens=12", "password=[REDACTED] prompt_tokens=12"),
        ):
            with self.subTest(source=source):
                once = redact(source)
                self.assertEqual(once, expected)
                self.assertEqual(redact(once), once)

    def test_marker_prefixed_authorization_redacts_the_entire_value(self):
        for scheme in ("", "Bearer ", "Basic ", "Custom "):
            for secret in ("后续秘密", "TAIL_SECRET", "[REDACTED]TAIL_SECRET"):
                source = f"Authorization: {scheme}[REDACTED]{secret}"
                with self.subTest(source=source):
                    once = redact({"messages": [source, json.dumps({"message": source})]})
                    expected = "Authorization: [REDACTED]"
                    self.assertEqual(once, {"messages": [expected, json.dumps({"message": expected})]})
                    self.assertEqual(redact(once), once)

    def test_marker_does_not_change_bare_value_boundaries(self):
        for key in ("password=", "Authorization: ", "Authorization: Bearer "):
            expected = "password=[REDACTED]" if key == "password=" else "Authorization: [REDACTED]"
            for prefix in ("alpha", "[REDACTED]"):
                for separator in (",", "]", "}", '"', "'", r'\"'):
                    source = f"{key}{prefix}{separator}TAIL_SECRET"
                    with self.subTest(source=source):
                        once = redact(source)
                        self.assertEqual(once, expected)
                        self.assertEqual(redact(once), once)

    def test_marker_does_not_swallow_a_following_quoted_credential(self):
        for source, expected in (
            ('password=[REDACTED],token="alpha TAIL_SECRET_9257"', 'password=[REDACTED],token="[REDACTED]"'),
            ("password=[REDACTED],clientSecret='alpha TAIL_SECRET_9257'", "password=[REDACTED],clientSecret='[REDACTED]'"),
            ('{"message":"password=[REDACTED]","token":"alpha TAIL_SECRET_9257"}',
             '{"message":"password=[REDACTED]","token":"[REDACTED]"}'),
            (r'{\"message\":\"password=[REDACTED]\",\"token\":\"alpha TAIL_SECRET_9257\"}',
             r'{\"message\":\"password=[REDACTED]\",\"token\":\"[REDACTED]\"}'),
            ('Authorization: Bearer abc,"password":"alpha TAIL_SECRET_9257"',
             'Authorization: [REDACTED],"password":"[REDACTED]"'),
            (r'password=[REDACTED],token=\"alpha TAIL_SECRET_9257\"', r'password=[REDACTED],token=\"[REDACTED]\"'),
        ):
            with self.subTest(source=source):
                once = redact(source)
                self.assertEqual(once, expected)
                self.assertEqual(redact(once), once)

    def test_json_message_preserves_following_ordinary_fields(self):
        for message in ("password=[REDACTED]", "Authorization: [REDACTED]", "password=[REDACTED]TAIL_SECRET"):
            for following in ({"prompt_tokens": 12}, {"note": "ordinary text"}, {"note": {"values": [12, "保留"]}}):
                payload = {"message": message, **following}
                source = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
                expected = {"message": message.replace("TAIL_SECRET", ""), **following}
                with self.subTest(payload=payload):
                    once = redact(source)
                    self.assertEqual(json.loads(once), expected)
                    self.assertEqual(redact(once), once)
                    if "TAIL_SECRET" not in source:
                        self.assertEqual(once, source)

    def test_agent_answer_and_trace_preserve_ordinary_json_fields(self):
        for message in ("password=[REDACTED]", "password=[REDACTED]TAIL_SECRET"):
            source = json.dumps({"message": message, "prompt_tokens": 12, "note": "ordinary text"}, separators=(",", ":"))

            class Model:
                name = "json-boundary-fixture"

                def complete(self, messages, tools):
                    return ModelDecision("final", source)

            with self.subTest(message=message), tempfile.TemporaryDirectory() as tmp:
                result = ResearchAgent(FailingSearch(), Model(), tmp).run("你好")
                events = {item["event"]: item for item in map(json.loads, Path(result.trace_path).read_text(encoding="utf-8").splitlines())}
                expected = {"message": "password=[REDACTED]", "prompt_tokens": 12, "note": "ordinary text"}
                self.assertEqual(result.status, "ok")
                for value in (result.answer, events["model_response"]["raw_text_truncated"], events["final_validated"]["answer"]):
                    self.assertEqual(json.loads(value), expected)

    def test_wrapped_json_and_credential_keys_in_answer_and_trace(self):
        secret = "sk-synthetic_key_927461"
        for payload, expected in (
            ({"message": "password=[REDACTED]TAIL_SECRET", "prompt_tokens": 12, "note": "ordinary text"},
             {"message": "password=[REDACTED]", "prompt_tokens": 12, "note": "ordinary text"}),
            ({secret: "ordinary text", "prompt_tokens": 12},
             {"sk-[REDACTED]": "ordinary text", "prompt_tokens": 12}),
            ({"nested": [{"Bearer " + secret: 12}], "note": "ordinary text"},
             {"nested": [{"Bearer [REDACTED]": 12}], "note": "ordinary text"}),
        ):
            for prefix, suffix in (("", ""), ("Result: ", " done"), ("```json\n", "\n```"),
                                   ("说明：\n```json\n", "\n```\n结束")):
                source = prefix + json.dumps(payload, separators=(",", ":")) + suffix
                wanted = prefix + json.dumps(expected, separators=(",", ":")) + suffix

                class Model:
                    name = "wrapped-json-boundary-fixture"

                    def complete(self, messages, tools):
                        return ModelDecision("final", source)

                with self.subTest(source=source), tempfile.TemporaryDirectory() as tmp:
                    self.assertEqual(redact(source), wanted)
                    self.assertEqual(redact(wanted), wanted)
                    result = ResearchAgent(FailingSearch(), Model(), tmp).run("你好")
                    trace_text = Path(result.trace_path).read_text(encoding="utf-8")
                    events = {item["event"]: item for item in map(json.loads, trace_text.splitlines())}
                    self.assertEqual(result.status, "ok")
                    for value in (result.answer, events["model_response"]["raw_text_truncated"], events["final_validated"]["answer"]):
                        self.assertEqual(value, wanted)
                    self.assertNotIn(secret, trace_text)
                    self.assertNotIn("TAIL_SECRET", trace_text)

    def test_structured_keys_and_escaped_json_keys_are_redacted(self):
        for key, cleaned in (("sk-synthetic_key_927461", "sk-[REDACTED]"),
                             ("password=synthetic927461", "password=[REDACTED]"),
                             ("https://user:TAIL_SECRET@example.com", "https://[REDACTED]@example.com")):
            payload = {key: {"note": "keep"}, "prompt_tokens": 12}
            expected = {cleaned: {"note": "keep"}, "prompt_tokens": 12}
            with self.subTest(key=key):
                self.assertEqual(redact(payload), expected)
                source = json.dumps(payload).replace("sk-", r"\u0073k-")
                self.assertEqual(json.loads(redact(source)), expected)
                escaped = json.dumps(source)[1:-1]
                self.assertEqual(json.loads(json.loads('"' + redact(escaped) + '"')), expected)

    def test_json_fragments_preserve_plaintext_credential_boundaries(self):
        payload = '{"message":"password=[REDACTED]TAIL_SECRET","note":12}'
        cleaned = '{"message":"password=[REDACTED]","note":12}'
        for source, expected in (
            (f"first: {payload} second: [{payload}]", f"first: {cleaned} second: [{cleaned}]"),
            (f'password="alpha TAIL_SECRET" {payload}', f'password="[REDACTED]" {cleaned}'),
            ('token={"private":"TAIL_SECRET"}', 'token=[REDACTED]'),
            (f'Authorization: Bearer TAIL_SECRET\n{payload}', f'Authorization: [REDACTED]\n{cleaned}'),
            (r'Result: {\"message\":\"password=[REDACTED]TAIL_SECRET\",\"note\":12}',
             r'Result: {\"message\":\"password=[REDACTED]\",\"note\":12}'),
        ):
            with self.subTest(source=source):
                self.assertEqual(redact(source), expected)
                self.assertEqual(redact(expected), expected)

    def test_json_shaped_url_and_authorization_credentials_do_not_leak(self):
        for source, secret in (
            ("https://user:[123456789]@example.com", "123456789"),
            ("Authorization: Bearer [123456789]", "123456789"),
            ('Authorization: "alpha [123456789]"', "123456789"),
            ("Authorization: 'alpha [123456789]'", "123456789"),
            ('Authorization: Custom {"value":"SYNTHETIC_PASS_123456"}', "SYNTHETIC_PASS_123456"),
            ('Authorization: Custom {"value":[123456789]}', "123456789"),
            ('Authorization: Bearer {"value":{"inner":"SYNTHETIC_PASS_123456"}}', "SYNTHETIC_PASS_123456"),
        ):
            class Model:
                name = "credential-json-shape-fixture"

                def complete(self, messages, tools):
                    return ModelDecision("final", source)

            with self.subTest(source=source), tempfile.TemporaryDirectory() as tmp:
                cleaned = redact(source)
                self.assertNotIn(secret, cleaned)
                self.assertEqual(redact(cleaned), cleaned)
                result = ResearchAgent(FailingSearch(), Model(), tmp).run("你好")
                self.assertEqual(result.status, "ok")
                self.assertEqual(result.answer, cleaned)
                self.assertNotIn(secret, Path(result.trace_path).read_text(encoding="utf-8"))

    def test_wrapped_json_strings_and_escaped_markdown_preserve_fields(self):
        source = json.dumps({"message": "password=[REDACTED]TAIL_SECRET", "prompt_tokens": 12,
                             "note": r"ordinary\ntext"}, separators=(",", ":"))
        expected = source.replace("TAIL_SECRET", "")
        for encode in (json.dumps, lambda text: json.dumps(text)[1:-1]):
            for prefix, suffix in (("Result: ", " done"), ("```json\n", "\n```")):
                with self.subTest(prefix=prefix, encoded=encode(source)):
                    wanted = prefix + encode(expected) + suffix
                    self.assertEqual(redact(prefix + encode(source) + suffix), wanted)
                    self.assertEqual(redact(wanted), wanted)

    def test_json_spans_preserve_format_duplicates_and_escaped_fields(self):
        for source, expected in (
            (' { "message" : "password=[REDACTED]TAIL_SECRET", "note" : "ok", "prompt_tokens" : 12 } ',
             ' { "message" : "password=[REDACTED]", "note" : "ok", "prompt_tokens" : 12 } '),
            ('{"message":"password=[REDACTED]TAIL_SECRET","message":"safe","note":12}',
             '{"message":"password=[REDACTED]","message":"safe","note":12}'),
            ('{"token":{"nested":["TAIL_SECRET"]},"note":12}', '{"token":"[REDACTED]","note":12}'),
            (r'{"mess\u0061ge":"password=[REDACTED]TAIL_SECRET","n\u006fte":12}',
             r'{"mess\u0061ge":"password=[REDACTED]","n\u006fte":12}'),
            (r'{\"message\":\"password=[REDACTED]TAIL_SECRET\",\"prompt_tokens\":12}',
             r'{\"message\":\"password=[REDACTED]\",\"prompt_tokens\":12}'),
            ('["password=[REDACTED]TAIL_SECRET",{"prompt_tokens":12}]',
             '["password=[REDACTED]",{"prompt_tokens":12}]'),
            ('{"message":"password=TAIL_SECRET","count":' + "1" * 5000 + '}',
             '{"message":"password=[REDACTED]","count":' + "1" * 5000 + '}'),
            (r'{"message":"password=TAIL_SECRET ordinary\ud800","prompt_tokens":12}',
             r'{"message":"password=[REDACTED] ordinary\ud800","prompt_tokens":12}'),
        ):
            with self.subTest(source=source):
                self.assertEqual(redact(source), expected)
                self.assertEqual(redact(expected), expected)

    def test_model_answer_and_trace_remove_marker_prefixed_secrets(self):
        class Model:
            name = "redaction-boundary-fixture"

            def complete(self, messages, tools):
                return ModelDecision(
                    "final",
                    "password=[REDACTED]后续秘密\nAuthorization: Bearer [REDACTED]TAIL_SECRET\n"
                    "password=[REDACTED],DELIMITER_SECRET\nAuthorization: [REDACTED]]AUTH_DELIMITER_SECRET\n"
                    'password=[REDACTED],token="alpha TAIL_SECRET_9257"',
                )

        expected = (
            "password=[REDACTED]\nAuthorization: [REDACTED]\npassword=[REDACTED]\nAuthorization: [REDACTED]\n"
            'password=[REDACTED],token="[REDACTED]"'
        )
        with tempfile.TemporaryDirectory() as tmp:
            result = ResearchAgent(FailingSearch(), Model(), tmp).run("你好")
            trace_text = Path(result.trace_path).read_text(encoding="utf-8")

        self.assertEqual(result.status, "ok")
        self.assertEqual(result.termination, "model_final")
        self.assertEqual(result.answer, expected)
        for secret in ("后续秘密", "TAIL_SECRET", "DELIMITER_SECRET", "AUTH_DELIMITER_SECRET", "TAIL_SECRET_9257"):
            self.assertNotIn(secret, trace_text)
        events = {item["event"]: item for item in map(json.loads, trace_text.splitlines())}
        self.assertEqual(events["model_response"]["raw_text_truncated"], expected)
        self.assertEqual(events["final_validated"]["answer"], expected)


if __name__ == "__main__":
    unittest.main()
