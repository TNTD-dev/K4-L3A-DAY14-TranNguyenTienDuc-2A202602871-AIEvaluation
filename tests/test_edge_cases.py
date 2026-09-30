"""Additional contract tests; provided test_solution.py is unchanged."""

from collections import Counter
from template import (
    BenchmarkRunner,
    EvalResult,
    LLMJudge,
    QAPair,
    RAGASEvaluator,
    rerank_by_overlap,
)


def test_empty_metrics_and_optional_retrieval():
    evaluator = RAGASEvaluator()
    assert evaluator.evaluate_faithfulness("", "") == 1
    assert evaluator.evaluate_completeness("", "") == 1
    assert evaluator.evaluate_relevance("", "") == 1
    assert evaluator.evaluate_context_recall([], "evidence") == 0
    assert evaluator.evaluate_context_precision([], "") == 1
    result = evaluator.run_full_eval("a", "a", "a", "a")
    assert result.context_recall is None
    assert BenchmarkRunner().generate_report([])["avg_context_precision"] is None


def test_judge_invalid_and_partial_json():
    assert LLMJudge(lambda _: "invalid").score_response("q", "a", {"accuracy": "x"})[
        "scores"
    ] == {"accuracy": 0.5}
    scores = LLMJudge(
        lambda _: '{"scores":{"accuracy":2,"clarity":true}}'
    ).score_response("q", "a", {"accuracy": "x", "clarity": "y"})["scores"]
    assert scores == {"accuracy": 1, "clarity": 0.5}


def test_regression_boundary():
    def result(score):
        return EvalResult(QAPair("q", "a"), "a", score, score, score, True)

    runner = BenchmarkRunner()
    assert runner.run_regression([result(0.85)], [result(0.9)])["passed"]
    assert not runner.run_regression([result(0.849)], [result(0.9)])["passed"]


def test_reranking_preserves_duplicates_and_recall():
    contexts = ["noise", "Paris France", "noise"]
    before = contexts.copy()
    after = rerank_by_overlap(contexts, "Paris France")
    assert contexts == before
    assert Counter(after) == Counter(before)
    evaluator = RAGASEvaluator()
    assert evaluator.evaluate_context_recall(
        after, "Paris France"
    ) == evaluator.evaluate_context_recall(before, "Paris France")
