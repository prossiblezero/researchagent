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
            keep = {"README.md", ".env.example", "datasets/tasks.json",
                    "tests/fixtures/input.json", "evals/run_all.py", "docs/tutorial/README.md"}
            exclude = {".env", "evals/reports/run.json", "evals/archives/old.py",
                       "evals/live_report.json", "evals/build_control_report.py",
                       "docs/snapshot.zip", "data/state.db", "traces/session.jsonl"}
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
