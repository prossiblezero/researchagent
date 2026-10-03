"""Shared, auditable scoring helpers for the H1 evaluation boundary."""

from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit


SCORER_VERSION = "h1.4"
_CITATION_RE = re.compile(r"\[([SE])(\d+)\]")
_REQUIRED_CASE_FIELDS = {
    "id",
    "category",
    "difficulty",
    "task_type",
    "near_duplicate_group",
    "question",
    "reference_answer",
    "acceptable_variants",
    "evidence_requirements",
    "refusal_policy",
    "provenance",
}


def _value(item: Any, name: str, default: Any = "") -> Any:
    return item.get(name, default) if isinstance(item, dict) else getattr(item, name, default)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stable_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def evidence_snapshot_sha256(sources: Iterable[Any], evidence: Iterable[Any]) -> str:
    source_urls = {str(_value(item, "source_id")): str(_value(item, "url")) for item in sources}
    rows = [
        {
            "evidence_id": str(_value(item, "evidence_id")),
            "source_id": str(_value(item, "source_id")),
            "source_url": source_urls.get(str(_value(item, "source_id")), ""),
            "content_sha256": text_sha256(str(_value(item, "content"))),
        }
        for item in evidence
    ]
    rows.sort(key=lambda row: (row["evidence_id"], row["source_id"], row["source_url"], row["content_sha256"]))
    return stable_hash(rows)


def percentile(values: Iterable[float], percentage: float) -> float | None:
    """Nearest-rank percentile; the report records this method explicitly."""
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return None
    index = max(0, math.ceil(percentage * len(ordered)) - 1)
    return ordered[index]


def wilson_interval(successes: int, total: int) -> list[float] | None:
    if total <= 0:
        return None
    z = 1.959963984540054
    observed = successes / total
    denominator = 1 + z * z / total
    centre = (observed + z * z / (2 * total)) / denominator
    margin = z * math.sqrt(observed * (1 - observed) / total + z * z / (4 * total * total)) / denominator
    return [round(max(0.0, centre - margin), 4), round(min(1.0, centre + margin), 4)]


def _citation_counts(found: list[str], known: set[str]) -> dict[str, Any]:
    valid = [item for item in found if item in known]
    invalid = [item for item in found if item not in known]
    return {
        "valid_occurrences": len(valid),
        "invalid_occurrences": len(invalid),
        "total_occurrences": len(found),
        "valid_unique_ids": sorted(set(valid)),
        "invalid_unique_ids": sorted(set(invalid)),
        "id_validity_rate": len(valid) / len(found) if found else None,
    }


def citation_metrics(answer: str, source_ids: Iterable[str], evidence_ids: Iterable[str]) -> dict[str, Any]:
    matches = [(kind, f"{kind}{number}") for kind, number in _CITATION_RE.findall(answer)]
    return {
        "source": _citation_counts([item for kind, item in matches if kind == "S"], set(source_ids)),
        "evidence": _citation_counts([item for kind, item in matches if kind == "E"], set(evidence_ids)),
    }


def _domain(url: str) -> str:
    return (urlsplit(url).hostname or "").lower().removeprefix("www.")


def _claim_judgment(result_claims: list[Any], judgment: dict[str, Any]) -> tuple[int, int, bool]:
    actual = {
        str(_value(claim, "claim_id")): (
            text_sha256(str(_value(claim, "statement"))),
            sorted(str(item) for item in (_value(claim, "evidence_ids", []) or [])),
        )
        for claim in result_claims
        if str(_value(claim, "claim_id"))
    }
    judged: dict[str, str] = {}
    for claim in judgment.get("claims", []):
        if not isinstance(claim, dict):
            return 0, 0, False
        claim_id = str(claim.get("claim_id", ""))
        status = claim.get("status")
        identity = (
            str(claim.get("statement_sha256", "")),
            sorted(str(item) for item in (claim.get("evidence_ids", []) or [])),
        )
        if claim_id in judged or status not in {"SUPPORTED", "REFUTED", "INSUFFICIENT", "CONFLICTING"} or actual.get(claim_id) != identity:
            return 0, 0, False
        judged[claim_id] = status
    complete = set(judged) == set(actual)
    return sum(status == "SUPPORTED" for status in judged.values()), len(judged), complete


def score_result(case: dict[str, Any], result: Any, judgment: dict[str, Any] | None = None) -> dict[str, Any]:
    """Score only adjudicated quality; terms and domains remain diagnostics."""
    judgment = judgment or {}
    answer = str(_value(result, "answer"))
    sources = list(_value(result, "sources", []) or [])
    evidence = list(_value(result, "evidence", []) or [])
    source_urls = {str(_value(item, "source_id")): str(_value(item, "url")) for item in sources}
    evidence_sources = {str(_value(item, "evidence_id")): str(_value(item, "source_id")) for item in evidence}
    citations = citation_metrics(answer, source_urls, evidence_sources)

    grounded_sources = set(citations["source"]["valid_unique_ids"])
    orphan_evidence = [
        item for item in citations["evidence"]["valid_unique_ids"]
        if evidence_sources[item] not in source_urls
    ]
    grounded_sources.update(
        evidence_sources[item]
        for item in citations["evidence"]["valid_unique_ids"]
        if evidence_sources[item] in source_urls
    )
    cited_domains = {_domain(source_urls[item]) for item in grounded_sources if item in source_urls}
    requirements = case.get("evidence_requirements", {})
    required_domains = {str(item).lower().removeprefix("www.") for item in requirements.get("required_domains", [])}
    min_sources = int(requirements.get("min_sources", 0))
    citation_requirement_met = (
        len(grounded_sources) >= min_sources
        and not citations["source"]["invalid_occurrences"]
        and not citations["evidence"]["invalid_occurrences"]
        and not orphan_evidence
        and all(any(actual == wanted or actual.endswith("." + wanted) for actual in cited_domains) for wanted in required_domains)
    )
    citation_safety_met = not citations["source"]["invalid_occurrences"] and not citations["evidence"]["invalid_occurrences"] and not orphan_evidence

    answer_bound = (
        bool(judgment)
        and judgment.get("answer_sha256") == text_sha256(answer)
        and judgment.get("evidence_sha256") == evidence_snapshot_sha256(sources, evidence)
        and isinstance(judgment.get("reason"), str)
        and bool(judgment["reason"].strip())
    )
    answer_correct = judgment.get("answer_correct") if answer_bound and isinstance(judgment.get("answer_correct"), bool) else None
    refusal_correct = judgment.get("refusal_correct") if answer_bound and isinstance(judgment.get("refusal_correct"), bool) else None
    result_claims = list(_value(result, "claims", []) or [])
    supported_claims, judged_claims, claims_complete = _claim_judgment(result_claims, judgment) if answer_bound else (0, 0, False)
    if not claims_complete:
        supported_claims = judged_claims = 0
    claim_support_rate = supported_claims / judged_claims if judged_claims else None
    claim_required = bool(requirements.get("claim_support_required", True))
    claim_requirement_met = not claim_required or (bool(result_claims) and claims_complete and supported_claims == judged_claims)

    refusal_mode = str(case.get("refusal_policy", {}).get("mode", "forbidden"))
    refused = str(_value(result, "status", "")) != "ok" or answer.lstrip().upper().startswith("INSUFFICIENT")
    if refusal_mode == "required" or (refusal_mode == "allowed" and refused):
        task_pass = refused and refusal_correct and citation_safety_met if refusal_correct is not None else None
    elif answer_correct is None or (claim_required and result_claims and not claims_complete):
        task_pass = None
    else:
        task_pass = answer_correct and claim_requirement_met and citation_requirement_met and not refused

    variants = [str(item) for item in case.get("acceptable_variants", [])]
    return {
        "judgment_status": "JUDGED" if task_pass is not None else "UNJUDGED",
        "judge_version": str(judgment.get("judge_version", "")),
        "judgment_binding": "MATCHED" if answer_bound and (not result_claims or claims_complete) else ("MISMATCH" if judgment else "MISSING"),
        "answer_correct": answer_correct,
        "claim_support": {
            "supported": supported_claims,
            "total": judged_claims,
            "rate": claim_support_rate,
        },
        "refusal_correct": refusal_correct,
        "task_pass": task_pass,
        "citation_requirement_met": citation_requirement_met,
        "citations": citations,
        "diagnostics": {
            "acceptable_variant_hits": [item for item in variants if item.casefold() in answer.casefold()],
            "cited_domains": sorted(cited_domains),
            "required_domains": sorted(required_domains),
            "orphan_evidence_ids": orphan_evidence,
        },
    }


def aggregate_rows(rows: list[dict[str, Any]], expected_runs: int) -> dict[str, Any]:
    scores = [row["score"] for row in rows]
    judged = [score for score in scores if isinstance(score.get("task_pass"), bool)]
    passed = sum(score["task_pass"] is True for score in judged)
    answers = [score["answer_correct"] for score in scores if isinstance(score.get("answer_correct"), bool)]
    claims_supported = sum(score["claim_support"]["supported"] for score in scores)
    claims_total = sum(score["claim_support"]["total"] for score in scores)

    by_case: dict[str, dict[int, bool]] = {}
    for row in rows:
        value = row["score"].get("task_pass")
        if isinstance(value, bool):
            case_id = str(row["case_id"])
            run_index = int(row["run_index"])
            case_runs = by_case.setdefault(case_id, {})
            if run_index in case_runs:
                raise ValueError(f"duplicate scored run: {case_id}/{run_index}")
            case_runs[run_index] = value
    expected_indices = set(range(1, expected_runs + 1))
    complete_cases = [list(runs.values()) for runs in by_case.values() if set(runs) == expected_indices]
    any_passed = sum(any(values) for values in complete_cases)
    all_passed = sum(all(values) for values in complete_cases)

    source_valid = sum(score["citations"]["source"]["valid_occurrences"] for score in scores)
    source_total = sum(score["citations"]["source"]["total_occurrences"] for score in scores)
    evidence_valid = sum(score["citations"]["evidence"]["valid_occurrences"] for score in scores)
    evidence_total = sum(score["citations"]["evidence"]["total_occurrences"] for score in scores)
    latencies = [float(row["latency_s"]) for row in rows]

    return {
        "runs": len(rows),
        "judged_runs": len(judged),
        "unjudged_runs": len(rows) - len(judged),
        "task_success": {
            "mean_single_run_rate": round(passed / len(judged), 4) if judged else None,
            "mean_single_run_raw": [passed, len(judged)],
            "mean_single_run_wilson_95": wilson_interval(passed, len(judged)),
            "at_least_once_rate": round(any_passed / len(complete_cases), 4) if complete_cases else None,
            "at_least_once_raw": [any_passed, len(complete_cases)],
            "all_runs_rate": round(all_passed / len(complete_cases), 4) if complete_cases else None,
            "all_runs_raw": [all_passed, len(complete_cases)],
            "expected_runs_per_case": expected_runs,
        },
        "answer_correctness": {
            "rate": round(sum(answers) / len(answers), 4) if answers else None,
            "raw": [sum(answers), len(answers)],
        },
        "claim_support": {
            "rate": round(claims_supported / claims_total, 4) if claims_total else None,
            "raw": [claims_supported, claims_total],
        },
        "citation_id_validity": {
            "source_rate": round(source_valid / source_total, 4) if source_total else None,
            "source_raw": [source_valid, source_total],
            "evidence_rate": round(evidence_valid / evidence_total, 4) if evidence_total else None,
            "evidence_raw": [evidence_valid, evidence_total],
        },
        "latency_s": {
            "p50": round(percentile(latencies, 0.50) or 0.0, 4),
            "p95": round(percentile(latencies, 0.95) or 0.0, 4),
            "method": "nearest_rank",
        },
        "tokens": {
            key: sum(int(row.get("usage", {}).get(key, 0) or 0) for row in rows)
            for key in ("prompt_tokens", "completion_tokens", "total_tokens")
        },
    }


def validate_dataset(dataset: dict[str, Any], expected_count: int | None = None, expected_split: str | None = None) -> None:
    if dataset.get("schema_version") != "h1.v1" or dataset.get("frozen") is not True:
        raise ValueError("dataset must use frozen h1.v1 schema")
    if not str(dataset.get("dataset_version", "")).strip() or dataset.get("split") not in {"dev", "test"}:
        raise ValueError("dataset requires a version and dev/test split")
    if expected_split is not None and dataset.get("split") != expected_split:
        raise ValueError(f"expected {expected_split} split")
    if dataset.get("license") != "CC0-1.0" or dataset.get("provenance", {}).get("origin") != "self-authored":
        raise ValueError("dataset requires its declared self-authored CC0 provenance")
    cases = dataset.get("cases")
    if not isinstance(cases, list) or (expected_count is not None and len(cases) != expected_count):
        raise ValueError(f"dataset must contain exactly {expected_count} cases")
    ids: set[str] = set()
    questions: set[str] = set()
    for index, case in enumerate(cases):
        if not isinstance(case, dict) or _REQUIRED_CASE_FIELDS - case.keys():
            raise ValueError(f"case {index} is missing required fields")
        case_id = str(case["id"])
        question = " ".join(str(case["question"]).casefold().split())
        required_text = ("category", "difficulty", "task_type", "near_duplicate_group", "question", "reference_answer")
        if (
            not case_id
            or case_id in ids
            or not question
            or question in questions
            or any(not isinstance(case.get(name), str) or not case[name].strip() for name in required_text)
        ):
            raise ValueError(f"case {index} has a duplicate or empty id/question")
        variants = case["acceptable_variants"]
        evidence = case["evidence_requirements"]
        refusal = case["refusal_policy"]
        provenance = case["provenance"]
        if not isinstance(variants, list) or not variants or not all(isinstance(item, str) and item.strip() for item in variants):
            raise ValueError(f"case {case_id} needs non-empty acceptable_variants")
        if not isinstance(evidence, dict) or not isinstance(evidence.get("required_domains"), list):
            raise ValueError(f"case {case_id} has invalid evidence_requirements")
        if not isinstance(evidence.get("min_sources"), int) or evidence["min_sources"] < 0:
            raise ValueError(f"case {case_id} has invalid min_sources")
        if refusal.get("mode") not in {"forbidden", "required", "allowed"} or not refusal.get("reason"):
            raise ValueError(f"case {case_id} has invalid refusal_policy")
        source_url = urlsplit(str(provenance.get("source_url", "")))
        if (
            provenance.get("origin") != "self_authored"
            or source_url.scheme not in {"http", "https"}
            or not source_url.hostname
            or not str(provenance.get("source_version", "")).strip()
        ):
            raise ValueError(f"case {case_id} has invalid provenance")
        provenance_sources = provenance["sources"] if "sources" in provenance else [{"url": provenance.get("source_url"), "source_version": provenance.get("source_version")}]
        if not isinstance(provenance_sources, list):
            raise ValueError(f"case {case_id} has invalid provenance sources")
        document_urls: list[tuple[str, str, str]] = []
        source_domains: set[str] = set()
        for source in provenance_sources:
            parsed = urlsplit(str(source.get("url", ""))) if isinstance(source, dict) else urlsplit("")
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or not str(source.get("source_version", "")).strip():
                raise ValueError(f"case {case_id} has invalid provenance source")
            domain = parsed.hostname.lower().removeprefix("www.")
            document_urls.append((parsed.scheme.lower(), domain, parsed.path.rstrip("/")))
            source_domains.add(domain)
        required_domains = {str(item).lower().removeprefix("www.") for item in evidence["required_domains"]}
        if len(provenance_sources) < evidence["min_sources"] or len(document_urls) != len(set(document_urls)):
            raise ValueError(f"case {case_id} provenance sources do not satisfy min_sources")
        if any(not any(actual == required or actual.endswith("." + required) for actual in source_domains) for required in required_domains):
            raise ValueError(f"case {case_id} provenance sources do not cover required_domains")
        ids.add(case_id)
        questions.add(question)


def validate_split_independence(dev: dict[str, Any], test: dict[str, Any], max_similarity: float = 0.5) -> dict[str, Any]:
    validate_dataset(dev, expected_split="dev")
    validate_dataset(test, expected_split="test")
    dev_groups = {str(case["near_duplicate_group"]) for case in dev["cases"]}
    test_groups = {str(case["near_duplicate_group"]) for case in test["cases"]}
    if dev_groups & test_groups:
        raise ValueError("dev/test near_duplicate_group values overlap")
    closest = (0.0, "", "")
    for left in dev["cases"]:
        left_question = "".join(str(left["question"]).casefold().split())
        left_grams = {left_question[index:index + 3] for index in range(max(1, len(left_question) - 2))}
        for right in test["cases"]:
            right_question = "".join(str(right["question"]).casefold().split())
            right_grams = {right_question[index:index + 3] for index in range(max(1, len(right_question) - 2))}
            similarity = len(left_grams & right_grams) / max(1, len(left_grams | right_grams))
            if similarity > closest[0]:
                closest = (similarity, str(left["id"]), str(right["id"]))
    if closest[0] >= max_similarity:
        raise ValueError(f"dev/test questions exceed similarity threshold: {closest}")
    return {"method": "character_trigram_jaccard", "threshold": max_similarity, "maximum": round(closest[0], 4), "pair": [closest[1], closest[2]]}


def load_judgments(path: Path | None) -> tuple[str, dict[tuple[str, int], dict[str, Any]]]:
    if path is None:
        return "", {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    version = str(payload.get("judge_version", "")).strip()
    if not version:
        raise ValueError("judgments require judge_version")
    indexed: dict[tuple[str, int], dict[str, Any]] = {}
    for item in payload.get("judgments", []):
        if not isinstance(item, dict):
            continue
        key = (str(item.get("case_id", "")), int(item.get("run_index", 0)))
        if not key[0] or key[1] < 1 or key in indexed:
            raise ValueError("judgments contain an invalid or duplicate key")
        hashes = (item.get("answer_sha256"), item.get("evidence_sha256"))
        if any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value) for value in hashes):
            raise ValueError("each judgment requires lowercase SHA-256 answer and evidence hashes")
        if not isinstance(item.get("reason"), str) or not item["reason"].strip():
            raise ValueError("each judgment requires an adjudication reason")
        indexed[key] = {**item, "judge_version": version}
    return version, indexed
