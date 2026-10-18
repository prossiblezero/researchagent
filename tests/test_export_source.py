"""Publication must exclude force-tracked outputs and preserve current source bytes."""
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts.export_source import export


class SourceExportTests(unittest.TestCase):
    def test_first_publication_has_no_history_or_bulk_results(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "source"
            root.mkdir()
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            keep = {"requirements-router.txt", "config/decision_router.json", "config/decision_router_c1_4b.json",
                    "evals/build_intent_routing_v2.py", "evals/build_intent_routing_v3.py", "evals/calibrate_decision_router.py",
                    "evals/train_decision_router.py", "evals/run_decision_router_eval.py",
                    "evals/report_decision_router_v3.py",
                    "README.md", ".env.example", "datasets/tasks.json",
                    "tests/fixtures/input.json", "evals/run_all.py", "docs/tutorial/README.md",
                    "docs/index.md", "docs/_config.yml", "docs/_layouts/guide.html",
                    "docs/assets/guide.css",
                    "evals/run_intent_routing_eval.py", "evals/run_judge_calibration.py",
                    "evals/run_memory_learning_benchmark.py", "evals/run_qasper_memory.py",
                    "docs/research/intent-routing-results-20261012.json",
                    "docs/research/llm-judge-results-20261013.json",
                    "docs/research/decision-router-results-20261018.json", "docs/research/decision-router-v3-results-20261018.json",
                    "docs/research/decision-router-results-20261018-assets/training.png",
                    "docs/research/decision-router-results-20261018-assets/training-plot-data.json"}
            for group in ('B', 'C1', 'D', 'dev-C1', 'dev-D'):
                keep.update(f'docs/research/decision-router-results-20261018-assets/{group}/{name}'
                            for name in ('calibration.png', 'calibration-plot-data.json'))
            exclude = {".env", "evals/reports/run.json", "evals/archives/old.py",
                       "evals/live_report.json", "evals/build_control_report.py",
                       "docs/snapshot.zip", "data/state.db", "traces/session.jsonl",
                       "docs/private.html", "docs/assets/run.json", "docs/secret.yml",
                       "docs/research/unlisted-results.json"}
            for name in keep | exclude:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(name, encoding="utf-8")
            subprocess.run(["git", "add", "-f", "."], cwd=root, check=True)
            (root / "README.md").write_text("current worktree", encoding="utf-8")
            destination = root / "data/export"
            count, _ = export(root, destination)
            actual = {p.relative_to(destination).as_posix() for p in destination.rglob("*") if p.is_file()}
            self.assertEqual(actual, keep)
            self.assertEqual(count, len(keep))
            self.assertFalse((destination / ".git").exists())
            self.assertEqual((destination / "README.md").read_text(encoding="utf-8"), "current worktree")
            with self.assertRaises(FileExistsError):
                export(root, destination)
            with self.assertRaises(ValueError):
                export(root, root / "datasets/export")


if __name__ == "__main__":
    unittest.main()
