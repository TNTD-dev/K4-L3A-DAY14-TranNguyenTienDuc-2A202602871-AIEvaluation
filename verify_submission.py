"""Offline verification of submission outputs, provenance and reranking invariants."""

from __future__ import annotations
import hashlib
import json
import math
from collections import Counter
from pathlib import Path
from template import RAGASEvaluator, rerank_by_overlap


def read(name: str) -> dict:
    return json.loads(Path(name).read_text())


def main() -> None:
    golden = read("golden_dataset.json")
    actual = read("artifacts/actual_answers.json")
    benchmark = read("artifacts/benchmark_results.json")
    comparison = read("artifacts/framework_comparison.json")
    ranking = read("artifacts/reranking_results.json")
    judge = read("artifacts/rubric_judge.json")
    ids = {p["id"] for p in golden["qa_pairs"]}
    assert len(ids) == 20
    assert Path("template.py").read_bytes() == Path("solution/solution.py").read_bytes()
    for artifact, key in (
        (actual, "answers"),
        (benchmark, "results"),
        (ranking, "results"),
        (judge, "results"),
    ):
        assert len(artifact[key]) == 20 and {r["id"] for r in artifact[key]} == ids
    assert actual["agent"]["model"] == "gpt-4o-mini" and actual["agent"]["top_k"] == 5
    assert judge["model"] == comparison["judge_model"] == "gpt-5.6-luna"
    assert comparison["embedding_model"] == "text-embedding-3-small"
    assert (
        benchmark["provenance"]["golden_sha256"]
        == hashlib.sha256(Path("golden_dataset.json").read_bytes()).hexdigest()
    )
    assert (
        benchmark["provenance"]["actual_sha256"]
        == hashlib.sha256(
            Path("artifacts/actual_answers.json").read_bytes()
        ).hexdigest()
    )
    assert len(comparison["results"]) == 160
    keys = {(r["id"], r["framework"], r["metric"]) for r in comparison["results"]}
    metrics = {"faithfulness", "relevance", "context_recall", "context_precision"}
    assert keys == {
        (case, framework, metric)
        for case in ids
        for framework in ("ragas", "deepeval")
        for metric in metrics
    }
    for r in comparison["results"]:
        if r["error"] is not None:
            assert r["id"] == "A01" and r["framework"] == "ragas"
            assert r["metric"] in {
                "faithfulness",
                "context_recall",
                "context_precision",
            }
            assert r["status"] == "unsupported_input" and r["score"] is None
        else:
            assert math.isfinite(r["score"])
    for framework in ("ragas", "deepeval"):
        for metric in metrics:
            successful = [
                r["score"]
                for r in comparison["results"]
                if r["framework"] == framework
                and r["metric"] == metric
                and r["error"] is None
            ]
            aggregate = comparison["aggregate"][framework][metric]
            assert aggregate["count"] == len(successful)
            assert math.isclose(
                aggregate["mean"], sum(successful) / len(successful), abs_tol=1e-12
            )
    for r in judge["results"]:
        parsed = json.loads(r["reasoning"])
        parsed = parsed.get("scores", parsed)
        assert set(parsed) == set(judge["rubric"])
        assert r["scores"] == parsed
    assert benchmark["summary"]["passed"] == sum(
        r["passed"] for r in benchmark["results"]
    )
    assert math.isclose(
        benchmark["summary"]["pass_rate"], benchmark["summary"]["passed"] / 20
    )
    pairs = {p["id"]: p for p in golden["qa_pairs"]}
    results = {r["id"]: r for r in benchmark["results"]}
    reranked = {r["id"]: r for r in ranking["results"]}
    evaluator = RAGASEvaluator()
    for a in actual["answers"]:
        p = pairs[a["id"]]
        assert (
            a["question"] == p["question"]
            and not a["error"]
            and a["actual_answer"].strip()
        )
        chunks = [c["text"] for c in a["retrieved_contexts"]]
        contexts = "\n\n".join(c["text"] for c in p["contexts"])
        r = results[a["id"]]
        score = evaluator.run_full_eval(
            a["actual_answer"], p["question"], contexts, p["expected_answer"], chunks
        )
        for key in (
            "faithfulness",
            "relevance",
            "completeness",
            "context_recall",
            "context_precision",
        ):
            assert math.isclose(getattr(score, key), r[key], abs_tol=1e-12)
        ordered = rerank_by_overlap(chunks, p["question"])
        assert Counter(ordered) == Counter(chunks)
        entry = reranked[a["id"]]
        assert math.isclose(
            entry["recall_before"], entry["recall_after"], abs_tol=1e-12
        )
        assert math.isclose(
            entry["precision_after"],
            evaluator.evaluate_context_precision(ordered, p["expected_answer"]),
            abs_tol=1e-12,
        )
    print(
        "PASS: 20 answers, benchmark, rubric judge, reranking and 160 framework outcomes verified (3 explicit RAGAS N/A)."
    )


if __name__ == "__main__":
    main()
