"""Small stdlib loader for the frozen mixed benchmark."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DATASETS = ROOT / "datasets"
DATASET_NAMES = ("qasper", "scifact", "hotpotqa", "longmemeval", "project")


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_tasks(name: str) -> list[dict[str, Any]]:
    if name not in DATASET_NAMES:
        raise ValueError(f"unknown benchmark: {name}")
    directory = DATASETS / "project" if name == "project" else DATASETS / "open" / name
    rows = _read(directory / "tasks.json")
    if not isinstance(rows, list):
        raise ValueError(f"{name} tasks must be a list")
    return rows


def load_contexts(name: str, tasks: list[dict[str, Any]] | None = None) -> dict[str, dict[str, Any]]:
    tasks = tasks if tasks is not None else load_tasks(name)
    directory = DATASETS / "project" if name == "project" else DATASETS / "open" / name
    contexts: dict[str, dict[str, Any]] = {}
    if name in {"qasper", "scifact", "project"}:
        for row in _read(directory / ("contexts.json" if name == "project" else "corpus.json")):
            contexts[row["id"]] = row
    for task in tasks:
        for row in task.get("contexts", []):
            contexts[row["id"]] = row
    return contexts


def load_mixed() -> dict[str, list[dict[str, Any]]]:
    return {name: load_tasks(name) for name in DATASET_NAMES}
