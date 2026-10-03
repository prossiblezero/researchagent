import copy
import json
import unittest

from research_agent.context import build_context


def read_pair(index, length):
    call_id = f"read-{index}"
    return [
        {
            "role": "assistant",
            "tool_calls": [{
                "id": call_id,
                "type": "function",
                "function": {"name": "read", "arguments": "{}"},
            }],
        },
        {
            "role": "tool",
            "tool_call_id": call_id,
            "content": json.dumps({"UNTRUSTED_TOOL_DATA": {
                "kind": "read", "evidence_id": f"E{index}", "content": "x" * length,
            }}, ensure_ascii=False),
        },
    ]


class H3ContextBoundaryTests(unittest.TestCase):
    def build(self, messages, budget):
        return build_context(
            messages,
            max_context_tokens=budget,
            output_reserve_tokens=0,
            safety_margin_tokens=0,
            question="question",
        )

    def test_short_recent_reads_fit_without_larger_excerpt_markers(self):
        head = [
            {"role": "system", "content": "rules" + "s" * 1415},
            {"role": "user", "content": "question"},
        ]
        recent = read_pair(2, 25) + read_pair(3, 25)
        messages = head + read_pair(1, 3000) + recent
        original = copy.deepcopy(messages)

        built = self.build(messages, 693)

        self.assertEqual(built.error, "")
        self.assertEqual(built.messages, head + recent)
        self.assertEqual(built.after_bytes, 2079)
        self.assertEqual(built.estimated_input_tokens, 693)
        self.assertEqual(built.removed, ["tool_pair:read-1"])
        self.assertEqual(messages, original)

        rejected = self.build(messages, 692)
        self.assertEqual(rejected.error, "context_budget_exceeded")
        self.assertEqual(rejected.messages, [])

    def test_mixed_read_lengths_report_only_text_actually_removed(self):
        head = [
            {"role": "system", "content": "rules"},
            {"role": "user", "content": "question"},
        ]
        short_pair = read_pair(2, 25)
        long_pair = read_pair(3, 3000)

        built = self.build(head + short_pair + long_pair, 231)

        self.assertEqual(built.error, "")
        self.assertEqual(built.messages[:4], head + short_pair)
        self.assertEqual(built.messages[4], long_pair[0])
        self.assertEqual(built.messages[5]["tool_call_id"], "read-3")
        payload = json.loads(built.messages[5]["content"])["UNTRUSTED_TOOL_DATA"]
        self.assertEqual(payload["evidence_id"], "E3")
        self.assertEqual(payload["content"], "")
        self.assertEqual(payload["content_chars"], 3000)
        self.assertTrue(payload["context_excerpted"])
        self.assertEqual(built.after_bytes, 693)
        self.assertEqual(built.estimated_input_tokens, 231)
        self.assertEqual(built.removed, ["read_content_excerpt:read-3"])


if __name__ == "__main__":
    unittest.main()
