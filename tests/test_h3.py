import json
import unittest
from pathlib import Path

from evals.run_h3_eval import _loop_scenario, build_report
from research_agent.context import build_context
from research_agent.contracts import Claim
from research_agent.trace import redact


ROOT = Path(__file__).resolve().parents[1]


class H3GateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        baseline = json.loads((ROOT / "tests/fixtures/h3-baseline-v7.json").read_text(encoding="utf-8"))
        cls.baseline = baseline
        cls.report = build_report("unit-test", baseline)

    def test_reconstructed_baseline_exercises_reviewed_failures(self):
        self.assertEqual(self.baseline["label"], "h3-p2-review-rejected-before-v7")
        self.assertEqual(
            {item["id"] for item in self.baseline["rows"] if not item["passed"]},
            {"html_extraction", "evidence_separation", "credential_redaction", "loop_quality_guard"},
        )
        rows = {item["id"]: item for item in self.baseline["rows"]}
        html = {item["name"]: item["passed"] for item in rows["html_extraction"]["assertions"]}
        credentials = {item["name"]: item["passed"] for item in rows["credential_redaction"]["assertions"]}
        self.assertFalse(html["semantic_header_retained"])
        self.assertFalse(credentials["nested_assignments_removed"])
        self.assertFalse(credentials["escaped_inner_quote_removed"])
        self.assertTrue(credentials["unterminated_credentials_removed"])

    def test_all_behavior_probes_pass(self):
        for item in self.report["rows"]:
            with self.subTest(probe=item["id"]):
                failures = [assertion for assertion in item["assertions"] if not assertion["passed"]]
                if item['id']=='explicit_truncation':
                    # The archived H3 probe treated incomplete HTML as successful evidence.
                    # Keep that runner/score frozen; the repair deliberately rejects this response.
                    checks={a['name']:a for a in item['assertions']}
                    self.assertIs(checks['read_ok']['actual'],False)
                    self.assertEqual(checks['content_hash']['actual'],'')
                    self.assertEqual({a['name'] for a in failures},{'read_ok','content_hash'})
                    continue
                self.assertTrue(item["passed"], failures)

    def test_legacy_gate_does_not_claim_unchanged_truncation_contract(self):
        self.assertFalse(self.report['gate']['stage_passed'])
        self.assertFalse(self.report['gate']['comparison']['no_prior_pass_regressions'])
        self.assertTrue(self.report['gate']['comparison']['quality_strictly_better'])
        self.assertTrue(self.report['gate']['comparison']['below_historical_pre_h3_tokens'])

    def test_protected_claim_is_not_silently_truncated(self):
        built = build_context(
            [{"role": "system", "content": "rules"}, {"role": "user", "content": "question"}],
            max_context_tokens=800,
            output_reserve_tokens=100,
            safety_margin_tokens=100,
            claims=[Claim("C1", "x" * 2000 + "CLAIM_TAIL", status="UNVERIFIED")],
        )
        self.assertEqual(built.error, "context_budget_exceeded")
        self.assertEqual(built.messages, [])

    def test_read_evidence_is_not_duplicated_across_context_messages(self):
        _, model, _ = _loop_scenario()
        saw_read = False
        for request in model.requests:
            summaries = []
            for message in request["messages"]:
                if message.get("role") == "tool":
                    payload = json.loads(message["content"])
                    payload = payload["UNTRUSTED_TOOL_DATA"]
                    if payload.get("kind") == "read":
                        saw_read = True
                        summaries.append(payload["content"])
                elif message.get("role") == "assistant" and message.get("content"):
                    payload = json.loads(message["content"])
                    if "CONTEXT_STATE" in payload:
                        summaries.extend(
                            item.get("excerpt", item.get("summary", ""))
                            for item in payload["CONTEXT_STATE"]["EVIDENCE_MAP"]
                            if "excerpt" in item or "summary" in item
                        )
            self.assertEqual(len(summaries), len(set(summaries)))
        self.assertTrue(saw_read)

    def test_quality_guard_uses_visible_evidence(self):
        quality = next(item for item in self.report["rows"] if item["id"] == "loop_quality_guard")
        assertions = {item["name"]: item for item in quality["assertions"]}
        self.assertTrue(assertions["facts_visible_to_model"]["passed"])
        self.assertTrue(assertions["stripped_evidence_refuses"]["passed"])
        self.assertTrue(assertions["stripped_evidence_reaches_model_final"]["passed"])
        self.assertTrue(assertions["prefix_cascade_facts_visible"]["passed"])
        self.assertTrue(assertions["prefix_cascade_answer_derived"]["passed"])

    def test_trace_redacts_common_credential_names(self):
        secrets = ["client-value", "session-value", "consumer-value"]
        value = redact({
            "client_secret": secrets[0],
            "session_token": secrets[1],
            "consumer_key": secrets[2],
            "prompt_tokens": 12,
            "message": "client_secret=inline-value",
        })
        serialized = json.dumps(value)
        self.assertTrue(all(secret not in serialized for secret in [*secrets, "inline-value"]))
        self.assertEqual(value["prompt_tokens"], 12)


if __name__ == "__main__":
    unittest.main()
