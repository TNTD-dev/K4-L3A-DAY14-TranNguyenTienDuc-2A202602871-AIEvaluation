"""Compare native RAGAS/DeepEval metrics on recorded outputs; resume by input hash."""

from __future__ import annotations
import argparse
import asyncio
import hashlib
import json
import math
import os
import time
from importlib.metadata import version
from pathlib import Path
from typing import Any
from pydantic import BaseModel

os.environ["RAGAS_DO_NOT_TRACK"] = "true"
os.environ["DEEPEVAL_TELEMETRY_OPT_OUT"] = "1"
os.environ["LANGCHAIN_TRACING_V2"] = "false"

from dotenv import load_dotenv
from openai import AsyncOpenAI, OpenAI
from deepeval.models import DeepEvalBaseLLM
from deepeval.metrics import (
    AnswerRelevancyMetric,
    FaithfulnessMetric,
    ContextualRecallMetric,
    ContextualPrecisionMetric,
)
from deepeval.test_case import LLMTestCase
from ragas.llms.base import InstructorBaseRagasLLM
from ragas.embeddings import OpenAIEmbeddings
from ragas.metrics.collections import (
    Faithfulness,
    AnswerRelevancy,
    ContextRecall,
    ContextPrecision,
)


class ResponsesJudge(DeepEvalBaseLLM):
    """Use the explicitly selected API model without DeepEval's model-name catalogue."""

    def __init__(self, model: str):
        self.model_name = model
        self.client = OpenAI()
        self.async_client = AsyncOpenAI()

    def load_model(self) -> OpenAI:
        return self.client

    def get_model_name(self) -> str:
        return self.model_name

    def generate(self, prompt: str, schema: type[BaseModel] | None = None) -> Any:
        if schema is not None:
            result = self.client.responses.parse(
                model=self.model_name, input=prompt, text_format=schema
            )
            return result.output_parsed
        return self.client.responses.create(
            model=self.model_name, input=prompt
        ).output_text

    async def a_generate(
        self, prompt: str, schema: type[BaseModel] | None = None
    ) -> Any:
        if schema is not None:
            result = await self.async_client.responses.parse(
                model=self.model_name, input=prompt, text_format=schema
            )
            return result.output_parsed
        return (
            await self.async_client.responses.create(
                model=self.model_name, input=prompt
            )
        ).output_text


class RagasResponsesJudge(InstructorBaseRagasLLM):
    """Native RAGAS structured-output interface backed by the Responses API."""

    def __init__(self, judge: ResponsesJudge):
        self.judge = judge

    def generate(self, prompt: str, response_model: type[BaseModel]) -> BaseModel:
        return self.judge.generate(prompt, response_model)

    async def agenerate(
        self, prompt: str, response_model: type[BaseModel]
    ) -> BaseModel:
        return await self.judge.a_generate(prompt, response_model)


def save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    )
    temporary.replace(path)


async def run(args: argparse.Namespace) -> int:
    golden = json.loads(args.golden.read_text())
    actual = json.loads(args.actual.read_text())
    if golden["corpus_id"] != actual["corpus_id"]:
        raise ValueError("Corpus mismatch")
    fingerprint = hashlib.sha256(
        args.golden.read_bytes()
        + args.actual.read_bytes()
        + args.model.encode()
        + args.embedding_model.encode()
    ).hexdigest()
    data = {
        "input_hash": fingerprint,
        "generator_model": actual["agent"]["model"],
        "judge_model": args.model,
        "embedding_model": args.embedding_model,
        "versions": {p: version(p) for p in ("ragas", "deepeval", "openai")},
        "results": [],
    }
    if args.output.exists():
        data = json.loads(args.output.read_text())
        if data["input_hash"] != fingerprint:
            raise ValueError(
                "Existing checkpoint inputs differ; use another output path"
            )
    # Fail before full evaluation if the requested model is unavailable.
    judge = ResponsesJudge(args.model)
    judge.client.responses.create(
        model=args.model,
        input='Return JSON {"ok":true}',
        text={"format": {"type": "json_object"}},
        max_output_tokens=64,
    )
    client = AsyncOpenAI()
    llm = RagasResponsesJudge(judge)
    embeddings = OpenAIEmbeddings(client=client, model=args.embedding_model)
    ragas = {
        "faithfulness": Faithfulness(llm=llm),
        "relevance": AnswerRelevancy(llm=llm, embeddings=embeddings),
        "context_recall": ContextRecall(llm=llm),
        "context_precision": ContextPrecision(llm=llm),
    }
    classes = {
        "faithfulness": FaithfulnessMetric,
        "relevance": AnswerRelevancyMetric,
        "context_recall": ContextualRecallMetric,
        "context_precision": ContextualPrecisionMetric,
    }
    answers = {r["id"]: r for r in actual["answers"]}
    if set(answers) != {r["id"] for r in golden["qa_pairs"]}:
        raise ValueError("Golden/actual IDs differ")
    semaphore = asyncio.Semaphore(8)

    async def evaluate_one(pair: dict[str, Any], framework: str, name: str) -> None:
        record = answers[pair["id"]]
        if record["question"] != pair["question"] or record.get("error"):
            raise ValueError(f"Invalid actual record: {pair['id']}")
        chunks = [c["text"] for c in record["retrieved_contexts"]]
        case = LLMTestCase(
            input=pair["question"],
            actual_output=record["actual_answer"],
            expected_output=pair["expected_answer"],
            retrieval_context=chunks,
        )
        old = next(
            (
                r
                for r in data["results"]
                if (r["id"], r["framework"], r["metric"])
                == (pair["id"], framework, name)
            ),
            None,
        )
        if old and (old["error"] is None or old.get("status") == "unsupported_input"):
            return
        async with semaphore:
            start = time.monotonic()
            try:
                if framework == "ragas":
                    kwargs = {"user_input": pair["question"]}
                    if name in ("faithfulness", "relevance"):
                        kwargs["response"] = record["actual_answer"]
                    if name != "relevance":
                        kwargs["retrieved_contexts"] = chunks
                    if name.startswith("context_"):
                        kwargs["reference"] = pair["expected_answer"]
                    result = await ragas[name].ascore(**kwargs)
                    score, reason = float(result.value), str(result)
                else:
                    metric = classes[name](
                        model=judge, include_reason=True, async_mode=True
                    )
                    await metric.a_measure(case)
                    score, reason = float(metric.score), metric.reason
                if not math.isfinite(score):
                    raise ValueError("Non-finite metric score")
                error = None
            except Exception as exc:
                score, reason, error = None, None, f"{type(exc).__name__}: {exc}"
            entry = {
                "id": pair["id"],
                "framework": framework,
                "metric": name,
                "score": score,
                "reason": reason,
                "error": error,
                "elapsed_seconds": time.monotonic() - start,
            }
            entry["status"] = (
                "scored"
                if error is None
                else (
                    "unsupported_input"
                    if not chunks
                    and framework == "ragas"
                    and name != "relevance"
                    and isinstance(error, str)
                    and ("retrieved_contexts" in error)
                    else "failed"
                )
            )
            if old:
                data["results"].remove(old)
            data["results"].append(entry)
            save(args.output, data)
            print(pair["id"], framework, name, score, error or "", flush=True)

    await asyncio.gather(
        *(
            evaluate_one(pair, framework, name)
            for pair in golden["qa_pairs"]
            for framework in ("ragas", "deepeval")
            for name in ragas
        )
    )
    data["aggregate"] = {}
    for framework in ("ragas", "deepeval"):
        data["aggregate"][framework] = {}
        for name in ragas:
            rows = [
                r
                for r in data["results"]
                if r["framework"] == framework
                and r["metric"] == name
                and r["error"] is None
            ]
            data["aggregate"][framework][name] = {
                "count": len(rows),
                "mean": sum(r["score"] for r in rows) / len(rows) if rows else None,
            }
    data["complete"] = len(data["results"]) == 160 and all(
        r["error"] is None or r.get("status") == "unsupported_input"
        for r in data["results"]
    )
    data["fully_scored"] = all(r["error"] is None for r in data["results"])
    save(args.output, data)
    return 0 if data["complete"] else 2


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--golden", type=Path, default=Path("golden_dataset.json"))
    parser.add_argument(
        "--actual", type=Path, default=Path("artifacts/actual_answers.json")
    )
    parser.add_argument(
        "--output", type=Path, default=Path("artifacts/framework_comparison.json")
    )
    parser.add_argument("--model", default=os.getenv("JUDGE_MODEL", "gpt-5.6-luna"))
    parser.add_argument("--embedding-model", default="text-embedding-3-small")
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
