import tempfile
import unittest
from pathlib import Path

from research_agent import Claim, Evidence, Experience, RunResult, RunStore, Source, AuditEvent


class StorageTests(unittest.TestCase):
    def test_save_and_reload_run_with_children(self):
        with tempfile.TemporaryDirectory() as tmp:
            trace = Path(tmp) / "trace.jsonl"
            trace.write_text("{}\n", encoding="utf-8")
            result = RunResult(
                "answer", [Source("S1", "title", "https://example.com", "snippet")], str(trace), "ok", "model_final", 1, ["S1"], [],
                [Evidence("E1", "S1", "evidence")], [Claim("C1", "claim", ["E1"], "SUPPORTED", 0.9, "matched")],
                [Experience(0, "search", "found", "good", "keep", "final")], [AuditEvent("before_tool", "allow")],
            )
            store = RunStore(Path(tmp) / "runs.db")
            run_id = store.save(result, "question", "2026-01-01T00:00:00+00:00")
            loaded = store.get(run_id)
            self.assertEqual(loaded["question"], "question")
            self.assertEqual(len(loaded["sources"]), 1)
            self.assertEqual(loaded["claims"][0]["evidence_ids"], ["E1"])
            self.assertEqual(len(store.list_runs()), 1)


if __name__ == "__main__":
    unittest.main()
