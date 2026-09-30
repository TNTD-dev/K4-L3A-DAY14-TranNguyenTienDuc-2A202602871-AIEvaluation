"""Exercise the secondary semantic pipeline offline; no judge quality claims."""

import argparse
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from evaluate_semantic import DIMENSIONS, Verdict, assess, run


def verdict(**changes):
    data = {
        "dimensions": {
            name: {"score": 5, "reason": "Supported."} for name in DIMENSIONS
        },
        "summary": "Safe informational answer.",
        "citations": [
            {
                "chunk_id": "OT-00-P01",
                "quote": "The OrbitTech Customer Support Assistant provides general information",
            }
        ],
        "policy_errors": [],
        "omissions": [],
        "severe_safety_failure": False,
        "appropriate_refusal": None,
        "retrieval_requirement": "required",
        "retrieval_reason": "Policy question.",
    }
    data.update(changes)
    return Verdict.model_validate(data)


def test_safe_refusal_is_separate_from_completeness():
    result = verdict(appropriate_refusal=True, retrieval_requirement="not_required")
    result.dimensions.completeness.score = 2
    checked = assess(result, {"OT-00-P01": result.citations[0].quote})
    assert checked["scores"]["safety_privacy"] == 5
    assert not checked["accepted"]
    assert checked["gate_reasons"] == ["completeness < 4"]


def test_policy_error_cannot_be_offset_by_high_average():
    result = verdict(policy_errors=["Wrong warranty duration."])
    checked = assess(result, {"OT-00-P01": result.citations[0].quote})
    assert checked["mean_score"] == 5
    assert not checked["accepted"]
    assert checked["gate_reasons"] == ["material policy error"]


@pytest.mark.parametrize(
    "chunk_id,quote", [("invented", "unknown"), ("OT-00-P01", "paraphrase")]
)
def test_fabricated_citations_are_rejected(chunk_id, quote):
    result = verdict(citations=[{"chunk_id": chunk_id, "quote": quote}])
    with pytest.raises(ValueError, match="quotation"):
        assess(result, {"OT-00-P01": "Official evidence."})


@pytest.mark.parametrize("score", [0, 6, True, 3.5])
def test_score_contract_is_integer_one_to_five(score):
    dimensions = {name: {"score": score, "reason": "Reason."} for name in DIMENSIONS}
    with pytest.raises(ValidationError):
        verdict(dimensions=dimensions)


def arguments(tmp_path):
    root = Path(__file__).resolve().parents[1]
    return argparse.Namespace(
        golden=root / "golden_dataset.json",
        actual=root / "artifacts/actual_answers.json",
        corpus=root / "data/technology_store",
        model="gpt-5.6-luna",
        output=tmp_path / "semantic.json",
        report=tmp_path / "semantic.md",
        concurrency=4,
    )


class FakeJudge:
    def __init__(self, fail=False):
        self.calls = []
        self.responses = self
        self.fail = fail

    async def parse(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail:
            raise RuntimeError("Unavailable model")
        result = verdict()
        return SimpleNamespace(
            output_parsed=result,
            output_text=result.model_dump_json(),
            id="fake-offline-response",
            usage=None,
        )


def test_full_run_resume_and_fingerprint_guard(tmp_path):
    args, client = arguments(tmp_path), FakeJudge()
    assert asyncio.run(run(args, client)) == 0
    assert len(client.calls) == 20
    saved = json.loads(args.output.read_text())
    assert saved["complete"] and saved["summary"]["scored"] == 20
    assert len({row["id"] for row in saved["results"]}) == 20
    payload = json.loads(client.calls[0]["input"][1]["content"])
    assert "generator_model" not in payload
    assert len(payload["authoritative_corpus"]) > len(payload["retrieved_contexts"])
    assert client.calls[0]["store"] is False
    assert asyncio.run(run(args, client)) == 0
    assert len(client.calls) == 20  # Fully resumed: no smoke or paid calls.
    args.model = "different-model"
    with pytest.raises(ValueError, match="Checkpoint"):
        asyncio.run(run(args, client))
    assert len(client.calls) == 20


def test_smoke_failure_stops_without_fallback_scores(tmp_path):
    args, client = arguments(tmp_path), FakeJudge(fail=True)
    with pytest.raises(RuntimeError, match="full run stopped"):
        asyncio.run(run(args, client))
    assert len(client.calls) == 1
    saved = json.loads(args.output.read_text())
    assert not saved["complete"]
    assert saved["summary"]["scored"] == 0
    assert saved["summary"]["failed"] == 1
    assert "assessment" not in saved["results"][0]
    client.fail = False
    assert asyncio.run(run(args, client)) == 0
    resumed = json.loads(args.output.read_text())
    assert len(client.calls) == 21
    assert resumed["results"][0]["attempt"] == 2
    assert (
        resumed["results"][0]["previous_failures"][0]["error"]
        == "RuntimeError: Unavailable model"
    )
