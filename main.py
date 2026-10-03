"""Command-line entry point for the stage 1 agent."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from research_agent import FailingSearch, FixtureReader, FixtureSearch, HttpReader, JsonSearch, ResearchAgent, TavilySearch, model_from_env
from research_agent.models import load_dotenv


def build_search():
    if os.getenv("OFFLINE_MODE", "").strip().lower() in {"1", "true", "yes", "on"}:
        return FixtureSearch.from_file(Path(__file__).with_name("fixtures") / "search_results.json")
    endpoint = os.getenv("SEARCH_URL")
    if endpoint:
        return JsonSearch(endpoint, os.getenv("SEARCH_API_KEY"))
    tavily_key = os.getenv("TAVILY_API_KEY") or os.getenv("SEARCH_API_KEY")
    if tavily_key:
        return TavilySearch(tavily_key, os.getenv("TAVILY_URL", "https://api.tavily.com/search"))
    return FixtureSearch.from_file(Path(__file__).with_name("fixtures") / "search_results.json")


def main(argv: list[str] | None = None) -> int:
    load_dotenv(Path(__file__).with_name(".env"))
    parser = argparse.ArgumentParser(description="Run the Stage 1 evidence-driven research loop")
    parser.add_argument("question", nargs="?", default="请查明 learn-claude-code 是什么，以及它的仓库地址。")
    parser.add_argument("--trace-dir", default="traces")
    parser.add_argument("--max-tool-calls", type=int, default=2)
    parser.add_argument("--max-rounds", type=int, default=None, help="maximum model rounds; defaults to tool budget + 3")
    parser.add_argument("--fail-search", action="store_true")
    args = parser.parse_args(argv)
    search = FailingSearch() if args.fail_search else build_search()
    offline = os.getenv("OFFLINE_MODE", "").strip().lower() in {"1", "true", "yes", "on"}
    reader = FixtureReader.from_file(Path(__file__).with_name("fixtures") / "search_results.json") if offline else (HttpReader() if os.getenv("TAVILY_API_KEY") or os.getenv("SEARCH_URL") else FixtureReader.from_file(Path(__file__).with_name("fixtures") / "search_results.json"))
    result = ResearchAgent(search, model_from_env(), trace_dir=args.trace_dir, max_tool_calls=args.max_tool_calls, reader=reader, max_rounds=args.max_rounds).run(args.question)
    print(result.answer)
    if result.sources:
        print("\nSources:")
        for source in result.sources:
            print(f"[{source.source_id}] {source.title} — {source.url}")
    print(f"\nTrace: {result.trace_path}")
    return 0 if result.status in {"ok", "insufficient"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
