"""Worker tests against the real database with a fake provider (labeled as such)."""

from __future__ import annotations

import json
from typing import Any

import httpx2 as httpx
import jsonschema
import openai
import psycopg
import pytest
from fakes import FakeProvider, add_urgency
from psycopg.rows import DictRow

from aicol.db import Contract
from aicol.jobs import Job
from aicol.providers import (
    ProviderError,
    classify,
    parse_json_object,
)
from aicol.schema import response_schema, validate_response
from aicol.worker import Worker


async def run_once(database_url: str, provider: FakeProvider, batch_size: int = 20) -> Any:
    contract = await Contract.connect(database_url)
    try:
        worker = Worker(contract, provider, worker_id="test", batch_size=batch_size)
        return await worker.run_once()
    finally:
        await contract.close()


async def test_worker_fills_llm_column_end_to_end(
    conn: psycopg.Connection[DictRow], ticket: str, database_url: str
) -> None:
    add_urgency(conn)
    provider = FakeProvider()
    stats = await run_once(database_url, provider)
    assert stats.claimed == 3 and stats.failed == 0 and stats.outcomes == {"written": 3}
    assert len(provider.calls) == 3
    rows = conn.execute("SELECT id, urgency FROM ticket ORDER BY id").fetchall()
    assert [r["urgency"] for r in rows] == ["high", "low", "low"]
    lineage = conn.execute(
        "SELECT model, confidence, usage FROM ai.result WHERE is_current ORDER BY row_pk"
    ).fetchall()
    assert {r["model"] for r in lineage} == {"fake/model"}
    assert lineage[0]["usage"]["cost"] == pytest.approx(0.00001)
    review = conn.execute("SELECT count(*) AS n FROM ai.needs_review").fetchone()
    assert review is not None and review["n"] == 2
    again = await run_once(database_url, provider)
    assert again.claimed == 0


async def test_worker_fills_embedding_column_in_one_call(
    conn: psycopg.Connection[DictRow], ticket: str, database_url: str
) -> None:
    conn.execute(
        "SELECT ai.add_column('ticket', 'embedding', array['body'], 'vector', p_backend => 'embedding',"
        " p_model => 'fake/embed', p_output_schema => '{\"dimensions\": 3}')"
    )
    provider = FakeProvider()
    stats = await run_once(database_url, provider)
    assert stats.outcomes == {"written": 3}
    assert len(provider.embed_calls) == 1 and len(provider.embed_calls[0]) == 3
    row = conn.execute("SELECT body, embedding::text AS e FROM ticket WHERE id = 3").fetchone()
    assert row is not None and row["e"] == f"[{len(row['body'])},1,0]"
    cost = conn.execute("SELECT results, cost FROM ai.cost_by_column").fetchone()
    assert (
        cost is not None and cost["results"] == 3 and float(cost["cost"]) == pytest.approx(0.000009)
    )


async def test_worker_splits_embedding_calls_by_backend_batch_size(
    conn: psycopg.Connection[DictRow], ticket: str, database_url: str
) -> None:
    conn.execute(
        "SELECT ai.add_column('ticket', 'embedding', array['body'], 'vector', p_backend => 'embedding',"
        " p_model => 'fake/embed', p_output_schema => '{\"dimensions\": 3}',"
        " p_backend_config => '{\"batch_size\": 2}')"
    )
    provider = FakeProvider()
    await run_once(database_url, provider)
    assert [len(c) for c in provider.embed_calls] == [2, 1]


async def test_worker_reports_failures_with_retry_decision(
    conn: psycopg.Connection[DictRow], ticket: str, database_url: str
) -> None:
    add_urgency(conn, max_attempts=3)
    request = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
    too_many = openai.RateLimitError(
        "429", response=httpx.Response(429, request=request), body=None
    )
    stats = await run_once(database_url, FakeProvider(fail_with=too_many))
    assert stats.failed == 3 and stats.rate_limited is True
    jobs = conn.execute("SELECT status, attempts, last_error FROM ai.job").fetchall()
    assert {j["status"] for j in jobs} == {"pending"} and {j["attempts"] for j in jobs} == {1}
    assert all("rate limited" in j["last_error"] for j in jobs)

    conn.execute("UPDATE ai.job SET next_attempt_at = now()")
    bad = openai.BadRequestError("400", response=httpx.Response(400, request=request), body=None)
    stats = await run_once(database_url, FakeProvider(fail_with=bad))
    assert stats.failed == 3 and stats.rate_limited is False
    assert {j["status"] for j in conn.execute("SELECT status FROM ai.job").fetchall()} == {"dead"}


async def test_worker_skips_custom_backend_jobs(
    conn: psycopg.Connection[DictRow], ticket: str, database_url: str
) -> None:
    conn.execute(
        "SELECT ai.add_column('ticket', 'geo', array['customer'], 'jsonb', p_backend => 'custom', p_model => 'geocoder')"
    )
    stats = await run_once(database_url, FakeProvider())
    assert stats.claimed == 0
    pending = conn.execute("SELECT count(*) AS n FROM ai.job WHERE status = 'pending'").fetchone()
    assert pending is not None and pending["n"] == 3


def test_classify_maps_sdk_errors() -> None:
    request = httpx.Request("POST", "https://x")
    assert classify(openai.APITimeoutError(request)).retryable is True
    assert (
        classify(
            openai.InternalServerError(
                "500", response=httpx.Response(500, request=request), body=None
            )
        ).retryable
        is True
    )
    assert (
        classify(
            openai.AuthenticationError(
                "401", response=httpx.Response(401, request=request), body=None
            )
        ).retryable
        is False
    )
    assert classify(json.JSONDecodeError("x", "", 0)).retryable is True
    assert classify(ProviderError("x", retryable=False)).retryable is False
    assert classify(RuntimeError("boom")).retryable is True


def test_response_schema_and_validation() -> None:
    schema = response_schema("enum", ["a", "b"])
    assert schema["properties"]["value"] == {"type": "string", "enum": ["a", "b"]}
    assert validate_response("enum", ["a", "b"], {"value": "a", "confidence": 0.5}) == ("a", 0.5)
    with pytest.raises(jsonschema.ValidationError):
        validate_response("enum", ["a", "b"], {"value": "c", "confidence": 0.5})
    assert response_schema("integer", {"min": 0, "max": 5})["properties"]["value"] == {
        "type": "integer",
        "minimum": 0,
        "maximum": 5,
    }
    custom = {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}
    assert response_schema("jsonb", custom)["properties"]["value"] == custom
    with pytest.raises(ValueError):
        response_schema("vector", {"dimensions": 3})


def test_source_text_rendering() -> None:
    def job(source: dict[str, Any]) -> Job:
        return Job(
            job_id=1,
            column_def_id=1,
            column_version_id=1,
            backend="llm",
            model="m",
            prompt="p",
            output_type="text",
            output_schema={},
            backend_config={},
            config={},
            row_pk={"id": 1},
            source_hash=b"",
            attempts=1,
            source=source,
        )

    assert job({"body": "ciao"}).source_text() == "ciao"
    assert (
        job({"title": "T", "tags": ["a", "b"], "n": None}).source_text()
        == 'title: T\ntags: ["a", "b"]\nn: '
    )


def test_parse_json_object_tolerates_trailing_text_and_fences() -> None:
    assert parse_json_object('{"value": "low", "confidence": 0.9}') == {
        "value": "low",
        "confidence": 0.9,
    }
    assert parse_json_object('{"value": "low", "confidence": 0.9}\n\nHope this helps!') == {
        "value": "low",
        "confidence": 0.9,
    }
    assert parse_json_object('```json\n{"value": 1, "confidence": 1}\n```') == {
        "value": 1,
        "confidence": 1,
    }
    with pytest.raises(json.JSONDecodeError):
        parse_json_object("not json at all")


async def test_worker_routes_decision_backend_and_stores_details(
    conn: psycopg.Connection[DictRow], ticket: str, database_url: str
) -> None:
    conn.execute(
        "SELECT ai.add_column('ticket', 'urgency', array['body'], 'enum', p_backend => 'decision',"
        " p_prompt => 'Urgenza', p_model => 'typesafe/jev-1.13',"
        ' p_output_schema => \'{"low": "routine", "high": "shop cannot sell"}\')'
    )
    provider = FakeProvider()
    stats = await run_once(database_url, provider)
    assert stats.outcomes == {"written": 3} and all(j.backend == "decision" for j in provider.calls)
    rows = conn.execute(
        "SELECT r.value, r.confidence, r.details FROM ai.result r WHERE r.is_current ORDER BY r.row_pk"
    ).fetchall()
    assert rows[0]["value"] == "high" and rows[0]["details"]["probabilities"]["high"] == 0.97
    assert conn.execute("SELECT urgency FROM ticket WHERE id = 1").fetchone() == {"urgency": "high"}


def test_decision_question_and_answer_mapping() -> None:
    from aicol.providers import decision_answer, decision_question

    def job(output_type: str, schema: Any, source: dict[str, Any] | None = None) -> Job:
        return Job(
            job_id=1,
            column_def_id=1,
            column_version_id=1,
            backend="decision",
            model="typesafe/jev-1.13",
            prompt="p",
            output_type=output_type,
            output_schema=schema,
            backend_config={},
            config={},
            row_pk={"id": 1},
            source_hash=b"",
            attempts=1,
            source=source or {"body": "x"},
        )

    q = decision_question(job("enum", {"a": "A", "b": "B"}))
    assert q == {"type": "choice", "instructions": "p", "criteria": {"a": "A", "b": "B"}}
    assert decision_question(job("boolean", {}))["type"] == "noul"
    assert decision_question(job("integer", {"levels": ["calm", "angry"]}))["criteria"] == [
        "calm",
        "angry",
    ]
    with pytest.raises(ProviderError):
        decision_question(job("text", {}))

    value, conf, details = decision_answer(
        job("enum", {"a": "A", "b": "B"}),
        {"choice": "b", "confidence": 0.8, "probabilities": {"a": 0.2, "b": 0.8}},
    )
    assert (value, conf, details["probabilities"]["b"]) == ("b", 0.8, 0.8)
    assert decision_answer(job("boolean", {}), {"noul": 0.3})[:2] == (False, 0.7)
    assert (
        decision_answer(
            job("integer", {"levels": ["a", "b", "c"]}), {"score": 1.6, "confidence": 0.6}
        )[0]
        == 2
    )
    assert (
        decision_answer(job("numeric", {"levels": ["a", "b"]}), {"score": 0.4, "confidence": 0.9})[
            0
        ]
        == 0.4
    )
    with pytest.raises(ProviderError):
        decision_answer(job("enum", {"a": "A"}), {"choice": "zzz", "confidence": 1})
