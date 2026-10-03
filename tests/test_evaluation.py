import json
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from evals.scoring import aggregate_rows, citation_metrics, evidence_snapshot_sha256, load_judgments, score_result, text_sha256, validate_dataset, validate_split_independence
from research_agent import Claim, Evidence, Source


ROOT = Path(__file__).resolve().parents[1]


def make_result(answer="Fact [S1] [E1].", status="ok", sources=None):
    statement = re.sub(r"\s*\[[ES]\d+\]", "", answer).strip()
    return SimpleNamespace(
        answer=answer,
        status=status,
        sources=sources or [Source("S1", "Source", "https://example.com/a", "Fact")],
        evidence=[Evidence("E1", "S1", "Fact")],
        claims=[Claim("C1", statement, ["E1"]) ] if "[E1]" in answer else [],
    )


def make_case(domain="example.com"):
    return {
        "acceptable_variants": ["Fact"],
        "evidence_requirements": {"min_sources": 1, "required_domains": [domain], "claim_support_required": True},
        "refusal_policy": {"mode": "forbidden", "reason": "Evidence is available."},
    }


def judgment(correct=True, result=None):
    result = result or make_result()
    return {
        "judge_version": "human-v1",
        "answer_sha256": text_sha256(result.answer),
        "evidence_sha256": evidence_snapshot_sha256(result.sources, result.evidence),
        "answer_correct": correct,
        "reason": "Test adjudication for this exact answer.",
        "claims": [{
            "claim_id": claim.claim_id,
            "statement_sha256": text_sha256(claim.statement),
            "evidence_ids": claim.evidence_ids,
            "status": "SUPPORTED" if correct else "REFUTED",
        } for claim in result.claims],
    }


class EvaluationTests(unittest.TestCase):
    def test_citation_validity_counts_occurrences(self):
        metrics = citation_metrics("[S1] then [S1] and [S9]", ["S1"], [])
        self.assertEqual(metrics["source"]["valid_occurrences"], 2)
        self.assertEqual(metrics["source"]["total_occurrences"], 3)
        self.assertEqual(metrics["source"]["id_validity_rate"], 2 / 3)

    def test_score_reparses_final_answer_instead_of_stale_result_field(self):
        result = make_result("Reviewed output without a citation")
        result.valid_citations = ["S1"]
        score = score_result(make_case(), result, judgment(result=result))
        self.assertFalse(score["citation_requirement_met"])
        self.assertFalse(score["task_pass"])

    def test_correctness_is_unjudged_without_versioned_judgment(self):
        score = score_result(make_case(), make_result(), None)
        self.assertEqual(score["judgment_status"], "UNJUDGED")
        self.assertIsNone(score["answer_correct"])
        self.assertIsNone(score["task_pass"])

    def test_terms_are_diagnostic_not_correctness(self):
        case = make_case()
        case["acceptable_variants"] = ["repository"]
        synonym_result = make_result("Official distribution index [S1] [E1].")
        wrong_result = make_result("Fact is explicitly false [S1] [E1].")
        synonym = score_result(case, synonym_result, judgment(True, synonym_result))
        wrong = score_result(case, wrong_result, judgment(False, wrong_result))
        self.assertTrue(synonym["task_pass"])
        self.assertEqual(synonym["diagnostics"]["acceptable_variant_hits"], [])
        self.assertFalse(wrong["task_pass"])

    def test_required_domain_must_be_cited(self):
        sources = [
            Source("S1", "Other", "https://example.com/a", "Fact"),
            Source("S2", "Expected", "https://pypi.org/project/a", "Fact"),
        ]
        result = make_result("Fact [S1] [E1].", sources=sources)
        score = score_result(make_case("pypi.org"), result, judgment(result=result))
        self.assertFalse(score["citation_requirement_met"])

    def test_orphan_evidence_cannot_satisfy_source_requirement(self):
        result = make_result("Fact [E1].")
        result.evidence = [Evidence("E1", "S999", "Fact")]
        result.claims[0].evidence_ids = ["E1"]
        score = score_result(make_case(), result, judgment(result=result))
        self.assertFalse(score["citation_requirement_met"])
        self.assertEqual(score["diagnostics"]["orphan_evidence_ids"], ["E1"])

    def test_judgment_must_match_exact_answer_and_all_claims(self):
        result = make_result()
        stale = judgment(result=result)
        stale["answer_sha256"] = "0" * 64
        self.assertIsNone(score_result(make_case(), result, stale)["task_pass"])

        result.claims.append(Claim("C2", "Second fact", ["E1"]))
        incomplete = judgment(result=result)
        incomplete["claims"].pop()
        score = score_result(make_case(), result, incomplete)
        self.assertEqual(score["judgment_binding"], "MISMATCH")
        self.assertIsNone(score["task_pass"])
        self.assertEqual(score["claim_support"]["total"], 0)

    def test_judgment_must_match_evidence_source_and_content_snapshot(self):
        for field, value in (("content", "Changed evidence"), ("url", "https://changed.example/a")):
            result = make_result()
            label = judgment(result=result)
            target = result.evidence[0] if field == "content" else result.sources[0]
            setattr(target, field, value)
            with self.subTest(field=field):
                score = score_result(make_case(), result, label)
                self.assertEqual(score["judgment_binding"], "MISMATCH")
                self.assertIsNone(score["task_pass"])

    def test_required_refusal_must_actually_refuse(self):
        case = make_case()
        case["evidence_requirements"]["claim_support_required"] = False
        case["refusal_policy"]["mode"] = "required"
        result = make_result(status="ok")
        label = judgment(result=result)
        label["refusal_correct"] = True
        self.assertFalse(score_result(case, result, label)["task_pass"])

        case["refusal_policy"]["mode"] = "allowed"
        refused = make_result("INSUFFICIENT: official source unavailable", status="insufficient", sources=[])
        refused.evidence = []
        label = judgment(result=refused)
        label["refusal_correct"] = True
        score = score_result(case, refused, label)
        self.assertFalse(score["citation_requirement_met"])
        self.assertTrue(score["task_pass"])

    def test_three_run_aggregation_reports_mean_any_and_all(self):
        results = [make_result(), make_result(), make_result()]
        scores = [score_result(make_case(), result, judgment(correct, result)) for result, correct in zip(results, (True, False, True))]
        rows = [{"case_id": "case-1", "run_index": index, "score": score, "latency_s": index, "usage": {}} for index, score in enumerate(scores, 1)]
        metrics = aggregate_rows(rows, expected_runs=3)
        self.assertEqual(metrics["task_success"]["mean_single_run_raw"], [2, 3])
        self.assertEqual(metrics["task_success"]["at_least_once_raw"], [1, 1])
        self.assertEqual(metrics["task_success"]["all_runs_raw"], [0, 1])
        self.assertEqual(metrics["latency_s"]["p95"], 3.0)

        duplicate = [dict(rows[0]), dict(rows[0]), dict(rows[2])]
        with self.assertRaises(ValueError):
            aggregate_rows(duplicate, expected_runs=3)

    def test_frozen_datasets_have_independent_50_100_splits(self):
        dev = json.loads((ROOT / "datasets" / "h1" / "h1_dev_cases.json").read_text(encoding="utf-8"))
        test = json.loads((ROOT / "datasets" / "h1" / "h1_test_cases.json").read_text(encoding="utf-8"))
        validate_dataset(dev, 50, "dev")
        validate_dataset(test, 100, "test")
        dev_questions = {" ".join(case["question"].casefold().split()) for case in dev["cases"]}
        test_questions = {" ".join(case["question"].casefold().split()) for case in test["cases"]}
        self.assertFalse(dev_questions & test_questions)
        self.assertFalse({case["near_duplicate_group"] for case in dev["cases"]} & {case["near_duplicate_group"] for case in test["cases"]})
        for dataset in (dev, test):
            categories = {case["category"] for case in dataset["cases"]}
            self.assertIn("security_attack", categories)
            self.assertIn("security_knowledge", categories)
        independence = validate_split_independence(dev, test)
        self.assertLess(independence["maximum"], independence["threshold"])

    def test_dataset_rejects_empty_reference_and_bad_top_level_metadata(self):
        dataset = json.loads((ROOT / "datasets" / "h1" / "h1_dev_cases.json").read_text(encoding="utf-8"))
        dataset["cases"][0]["reference_answer"] = ""
        with self.assertRaises(ValueError):
            validate_dataset(dataset, 50, "dev")
        dataset = json.loads((ROOT / "datasets" / "h1" / "h1_dev_cases.json").read_text(encoding="utf-8"))
        dataset["license"] = ""
        with self.assertRaises(ValueError):
            validate_dataset(dataset, 50, "dev")

    def test_dataset_rejects_inconsistent_provenance_sources(self):
        original = json.loads((ROOT / "datasets" / "h1" / "h1_dev_cases.json").read_text(encoding="utf-8"))
        case_index = next(index for index, case in enumerate(original["cases"]) if case["evidence_requirements"]["min_sources"] > 1)

        for name, mutate in (
            ("missing", lambda case: case["provenance"].pop("sources")),
            ("duplicate", lambda case: case["provenance"]["sources"][1].update(url=case["provenance"]["sources"][0]["url"].split("#", 1)[0] + "#duplicate")),
            ("domain gap", lambda case: case["evidence_requirements"].update(required_domains=["missing.example"])),
            ("missing version", lambda case: case["provenance"]["sources"][0].update(source_version="")),
        ):
            dataset = json.loads(json.dumps(original))
            mutate(dataset["cases"][case_index])
            with self.subTest(name=name), self.assertRaises(ValueError):
                validate_dataset(dataset, 50, "dev")

    def test_judgments_are_versioned_and_keyed_by_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "judgments.json"
            path.write_text(json.dumps({"judge_version": "human-v1", "judgments": [{"case_id": "a", "run_index": 2, "answer_sha256": "a" * 64, "evidence_sha256": "b" * 64, "answer_correct": True, "reason": "Reviewed manually."}]}), encoding="utf-8")
            version, indexed = load_judgments(path)
        self.assertEqual(version, "human-v1")
        self.assertTrue(indexed[("a", 2)]["answer_correct"])


if __name__ == "__main__":
    unittest.main()
