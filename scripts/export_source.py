"""Export current source and evaluation inputs without Git history or run outputs."""
import argparse
from pathlib import Path
import shutil
import subprocess


ROOT_FILES = set("""
.dockerignore .env.example .gitattributes .gitignore Dockerfile README.md main.py server.py
requirements.txt requirements-retrieval.lock.txt setup_retrieval.py
stage1_agent.py start-researchagent.ps1 scripts/export_source.py
""".split())
SOURCE_DIRS = {"research_agent", "web", "fixtures", "datasets", "tests", ".github"}
# Keep CI, research/evidence/agent benchmarks and all their shared test dependencies.
CORE_EVALS = set("""
agent_metrics audit_auto_research_results audit_scifact_predictions
auto_research_assessment postprocess_evidence_qa scoring
run_all run_stage1 run_metrics run_v1_eval run_v2_eval run_v3_retrieval
run_h2_eval run_h3_eval run_harness_strategies run_verification_recovery
run_agent_benchmark run_mixed_benchmark summarize_product_benchmark
run_evidence_qa_acceptance run_auto_research_acceptance run_amem_acceptance
run_parallel_research_acceptance prepare_locomo_case
""".split())


def included(name):
    path = Path(name)
    if any(p in {".git", "__pycache__", "reports", "archives"} for p in path.parts):
        return False
    if path.name.startswith(".env") and name != ".env.example":
        return False
    if path.suffix.lower() in {".log", ".zip", ".pyc", ".pyo", ".db", ".sqlite", ".sqlite3", ".patch"}:
        return False
    return (name in ROOT_FILES or path.parts[0] in SOURCE_DIRS
            or (path.parts[0] == "docs" and path.suffix == ".md")
            or (path.parent == Path("evals") and path.suffix == ".py" and path.stem in CORE_EVALS))


def export(root, destination):
    root, destination = root.resolve(), destination.resolve()
    if root.is_relative_to(destination) or (
        destination.is_relative_to(root) and not destination.is_relative_to(root / "data")
    ):
        raise ValueError("Export inside data/ or outside the source repository")
    if destination.exists():
        raise FileExistsError(f"Refusing to overwrite: {destination}")
    names = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=root
    ).decode("utf-8").split("\0")
    selected = sorted({name for name in names if name and included(name)})
    # Validate every input before creating the destination; reject linked files/directories.
    for name in selected:
        source = root / name
        if not source.is_file() or source.resolve() != source.absolute():
            raise ValueError(f"Missing or linked source: {name}")
        if not source.resolve().is_relative_to(root):
            raise ValueError(f"Source outside repository: {name}")
    destination.mkdir(parents=True, exist_ok=False)
    size = 0
    for name in selected:
        source, target = root / name, destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        size += target.stat().st_size
    return len(selected), size


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path, nargs="?", default=root / "data/github-publication")
    args = parser.parse_args()
    count, size = export(root, args.destination)
    print(f"Exported {count} files ({size:,} bytes) to {args.destination.resolve()}")
    print("No Git history copied. Initialize a NEW repository here for the first publication.")
