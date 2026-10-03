import json
import unittest

from research_agent.context import _excerpt, build_context


class H3PostAuditRegressionTests(unittest.TestCase):
    def test_large_search_payload_is_compacted_before_context_failure(self):
        messages = [
            {"role": "system", "content": "rules" + "s" * 22000},
            {"role": "user", "content": "find the relevant evidence"},
        ]
        for index in range(4):
            call_id = f"search-{index}"
            messages.extend([
                {"role": "assistant", "tool_calls": [{
                    "id": call_id, "type": "function",
                    "function": {"name": "search", "arguments": "{}"},
                }]},
                {"role": "tool", "tool_call_id": call_id, "content": json.dumps({
                    "UNTRUSTED_TOOL_DATA": {
                        "kind": "search",
                        "results": [{
                            "evidence_id": f"E{index}-{item}",
                            "source_id": f"S{index}-{item}",
                            "title": "title",
                            "url": f"https://example.com/{index}/{item}",
                            "snippet": "snippet " * 300,
                        } for item in range(5)],
                    }
                })},
            ])

        built = build_context(
            messages,
            max_context_tokens=16_384,
            output_reserve_tokens=2_048,
            safety_margin_tokens=256,
            question="find evidence",
        )

        self.assertEqual(built.error, "")
        self.assertLessEqual(built.estimated_input_tokens, built.input_limit_tokens)
        self.assertTrue(any("tool_pair:search-0" in item for item in built.removed))
        payload = json.loads(built.messages[-1]["content"])["UNTRUSTED_TOOL_DATA"]
        self.assertLessEqual(len(payload["results"][0]["snippet"]), 500)

    def test_tight_budget_drops_read_text_before_failing_protocol_skeleton(self):
        messages = [
            {"role": "system", "content": "rules"},
            {"role": "user", "content": "question"},
            {
                "role": "assistant",
                "tool_calls": [{
                    "id": "read-1",
                    "type": "function",
                    "function": {"name": "read", "arguments": '{"url":"https://example.com"}'},
                }],
            },
            {
                "role": "tool",
                "tool_call_id": "read-1",
                "content": json.dumps({
                    "UNTRUSTED_TOOL_DATA": {
                        "kind": "read",
                        "evidence_id": "E1",
                        "content": "x" * 1000,
                        "content_chars": 1000,
                    }
                }),
            },
        ]

        built = build_context(
            messages,
            max_context_tokens=269,
            output_reserve_tokens=50,
            safety_margin_tokens=50,
            question="question",
        )

        self.assertEqual(built.error, "")
        self.assertLessEqual(built.estimated_input_tokens, built.input_limit_tokens)
        self.assertIn("context_excerpted", json.dumps(built.messages))

    def test_chinese_question_selects_related_middle_evidence(self):
        body = "开头\n" + "无关内容\n" * 300 + "项目发布日期：2026年9月15日\n" + "无关内容\n" * 300 + "结尾"
        excerpt = _excerpt(body, "项目发布日期是什么？", 200)

        self.assertIn("2026年9月15日", excerpt)
        self.assertLessEqual(len(excerpt), 200)

    def test_repeated_query_terms_fall_back_to_head_middle_tail(self):
        repeated = "launch date is discussed but no value. " * 80
        body = repeated + "launch date: UNIQUE_VALUE_7843. " + repeated

        excerpt = _excerpt(body, "What is the launch date?", 320)

        self.assertIn("UNIQUE_VALUE_7843", excerpt)
        self.assertGreater(len(excerpt), 300)


if __name__ == "__main__":
    unittest.main()
