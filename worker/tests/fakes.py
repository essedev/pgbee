"""Test doubles shared by the worker tests. The fake provider is labeled as such in its model ids."""

from __future__ import annotations

import json
from typing import Any

import psycopg
from psycopg.rows import DictRow

from pgbee.jobs import Job
from pgbee.providers import EmbeddingResult, LlmResult, ProviderError

URGENCY = json.dumps(["low", "medium", "high"])


class FakeProvider:
    """Test double: derives by keyword, embeds by length. Records every call."""

    backends: tuple[str, ...] = ("llm", "decision", "embedding")

    def __init__(self, fail_with: Exception | None = None) -> None:
        self.calls: list[Job] = []
        self.embed_calls: list[list[str]] = []
        self.fail_with = fail_with
        self.decide_calls: list[list[Job]] = []
        self.unanswered_def: int | None = None

    async def derive(self, job: Job) -> LlmResult:
        self.calls.append(job)
        if self.fail_with is not None:
            raise self.fail_with
        text = job.source_text().lower()
        value = "high" if "giù" in text or "urgente" in text else "low"
        return LlmResult(
            value=value,
            confidence=0.95 if value == "high" else 0.6,
            model="fake/model",
            usage={"prompt_tokens": 10, "completion_tokens": 2, "cost": 0.00001},
            latency_ms=5,
        )

    async def decide(self, jobs: list[Job]) -> list[LlmResult | ProviderError]:
        self.calls.extend(jobs)
        self.decide_calls.append(jobs)
        if self.fail_with is not None:
            raise self.fail_with
        results: list[LlmResult | ProviderError] = []
        for job in jobs:
            if job.column_def_id == self.unanswered_def:
                results.append(ProviderError("decision model returned no answer", retryable=True))
                continue
            text = job.source_text().lower()
            high = "giù" in text or "urgente" in text
            value: Any = high if job.output_type == "boolean" else ("high" if high else "low")
            results.append(
                LlmResult(
                    value=value,
                    confidence=0.97 if high else 0.81,
                    model="typesafe/jev-1.13-fake",
                    usage={"prompt_tokens": 40 / len(jobs), "cost": 0.00000168 / len(jobs)},
                    latency_ms=90,
                    details={
                        "probabilities": {
                            "high": 0.97 if high else 0.19,
                            "low": 0.03 if high else 0.81,
                        },
                        "questions_in_call": len(jobs),
                    },
                )
            )
        return results

    async def embed(self, model: str, texts: list[str], config: dict[str, Any]) -> EmbeddingResult:
        self.embed_calls.append(texts)
        if self.fail_with is not None:
            raise self.fail_with
        return EmbeddingResult(
            vectors=[[float(len(t)), 1.0, 0.0] for t in texts],
            model=model,
            usage={"prompt_tokens": 3 * len(texts), "cost": 0.000003 * len(texts)},
            latency_ms=8,
        )


def add_urgency(conn: psycopg.Connection[DictRow], **config: Any) -> None:
    conn.execute(
        "SELECT bee.add_column('ticket', 'urgency', array['body'], 'enum',"
        " p_prompt => 'Classifica l''urgenza', p_model => 'anthropic/claude-haiku-4.5',"
        " p_output_schema => %s::jsonb, p_config => %s::jsonb)",
        (URGENCY, json.dumps(config)),
    )
