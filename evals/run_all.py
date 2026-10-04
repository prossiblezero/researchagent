"""Run all deterministic checks that do not require external APIs."""
from __future__ import annotations
import json, os, subprocess, sys, tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from research_agent import (FixtureSearch, OfflineModel, ResearchAgent, SearchResponse,
                            ModelDecision, Evidence, Claim, verify_claims)
from research_agent.policy import before_tool, before_finalize
commands = [[sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v" if os.getenv("CI") == "true" else "-q"], [sys.executable, "evals/run_stage1.py"], [sys.executable, "evals/run_metrics.py"]]
for command in commands:
    completed = subprocess.run(command, cwd=ROOT)
    if completed.returncode:
        raise SystemExit(completed.returncode)

def stage3_7_checks():
    # Stage 3: duplicate action and round budget are observable and bounded.
    class RepeatModel:
        name = "repeat-fixture"
        def complete(self, messages, tools):
            if any(m.get("role") == "tool" for m in messages):
                return ModelDecision("tool_call", query="same", call_id="repeat")
            return ModelDecision("tool_call", query="same", call_id="first")
    with tempfile.TemporaryDirectory() as tmp:
        result = ResearchAgent(FixtureSearch([]), RepeatModel(), tmp, max_tool_calls=3, max_rounds=4).run("查资料")
        assert result.termination == "duplicate_action"
    # Stage 4: deterministic verifier exposes all four states.
    def ev(i, text): return Evidence(i, "S1", text)
    assert verify_claims([Claim("C1", "python is fast", ["E1"])], [ev("E1", "python is fast")])[0].status == "SUPPORTED"
    assert verify_claims([Claim("C1", "python is fast", ["E1"])], [ev("E1", "python is not fast")])[0].status == "REFUTED"
    assert verify_claims([Claim("C1", "python is fast", ["E1"])], [ev("E1", "unrelated text")])[0].status == "INSUFFICIENT"
    assert verify_claims([Claim("C1", "python is fast", ["E1", "E2"])], [ev("E1", "python is fast"), ev("E2", "python is not fast")])[0].status == "CONFLICTING"
    # Stage 5/6: feedback and hooks are concrete, and unsafe actions are denied.
    with tempfile.TemporaryDirectory() as tmp:
        result = ResearchAgent(FixtureSearch.from_file(ROOT / "fixtures" / "search_results.json"), OfflineModel(), tmp, reader=None).run("请查明 learn-claude-code")
        assert result.experiences and result.experiences[0].feedback and result.experiences[0].next_action
    assert before_tool("search", "").decision == "recover"
    assert before_tool("read", "", "https://example.com/x", set()).reason == "read_not_authorized"
    assert before_finalize("普通回答").decision == "allow"
    print("Stage 3-7 eval: PASS")

stage3_7_checks()
print("All offline evaluations passed")
