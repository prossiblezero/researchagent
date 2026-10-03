"""Run the deterministic Stage 1 behavior evaluation set."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from research_agent import FailingSearch, FixtureSearch, OfflineModel, ResearchAgent


def main() -> int:
    cases = json.loads((ROOT / "datasets" / "harness" / "stage1_cases.json").read_text(encoding="utf-8"))
    passed = 0
    for case in cases:
        search = FailingSearch() if case["mode"] == "fail" else FixtureSearch.from_file(ROOT / "fixtures" / "search_results.json")
        with tempfile.TemporaryDirectory() as tmp:
            result = ResearchAgent(search, OfflineModel(), tmp).run(case["question"])
        expected = case["expect"]
        checks = [result.status == expected["status"], result.tool_calls == expected["tool_calls"]]
        if "contains" in expected:
            checks.append(expected["contains"] in result.answer)
        if "citation" in expected:
            checks.append(expected["citation"] in result.valid_citations)
        ok = all(checks)
        passed += int(ok)
        print(f"{'PASS' if ok else 'FAIL'} {case['id']}: status={result.status}, tool_calls={result.tool_calls}")
    print(f"Stage 1 eval: {passed}/{len(cases)} passed")
    return 0 if passed == len(cases) else 1


if __name__ == "__main__":
    raise SystemExit(main())
