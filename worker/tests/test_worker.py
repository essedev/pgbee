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

from pgbee.db import Contract
from pgbee.jobs import Job
from pgbee.providers import (
    ProviderError,
    classify,
    parse_json_object,
)
from pgbee.schema import response_schema, validate_response
from pgbee.worker import Worker


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
        "SELECT model, confidence, usage FROM bee.result WHERE is_current ORDER BY row_pk"
    ).fetchall()
    assert {r["model"] for r in lineage} == {"fake/model"}
    assert lineage[0]["usage"]["cost"] == pytest.approx(0.00001)
    review = conn.execute("SELECT count(*) AS n FROM bee.needs_review").fetchone()
    assert review is not None and review["n"] == 2
    again = await run_once(database_url, provider)
    assert again.claimed == 0


async def test_worker_fills_embedding_column_in_one_call(
    conn: psycopg.Connection[DictRow], ticket: str, database_url: str
) -> None:
    conn.execute(
        "SELECT bee.add_column('ticket', 'embedding', array['body'], 'vector', p_backend => 'embedding',"
        " p_model => 'fake/embed', p_output_schema => '{\"dimensions\": 3}')"
    )
    provider = FakeProvider()
    stats = await run_once(database_url, provider)
    assert stats.outcomes == {"written": 3}
    assert len(provider.embed_calls) == 1 and len(provider.embed_calls[0]) == 3
    row = conn.execute("SELECT body, embedding::text AS e FROM ticket WHERE id = 3").fetchone()
    assert row is not None and row["e"] == f"[{len(row['body'])},1,0]"
    cost = conn.execute("SELECT results, cost FROM bee.cost_by_column").fetchone()
    assert (
        cost is not None and cost["results"] == 3 and float(cost["cost"]) == pytest.approx(0.000009)
    )


async def test_worker_splits_embedding_calls_by_backend_batch_size(
    conn: psycopg.Connection[DictRow], ticket: str, database_url: str
) -> None:
    conn.execute(
        "SELECT bee.add_column('ticket', 'embedding', array['body'], 'vector', p_backend => 'embedding',"
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
    jobs = conn.execute("SELECT status, attempts, last_error FROM bee.job").fetchall()
    assert {j["status"] for j in jobs} == {"pending"} and {j["attempts"] for j in jobs} == {1}
    assert all("rate limited" in j["last_error"] for j in jobs)

    conn.execute("UPDATE bee.job SET next_attempt_at = now()")
    bad = openai.BadRequestError("400", response=httpx.Response(400, request=request), body=None)
    stats = await run_once(database_url, FakeProvider(fail_with=bad))
    assert stats.failed == 3 and stats.rate_limited is False
    assert {j["status"] for j in conn.execute("SELECT status FROM bee.job").fetchall()} == {"dead"}


async def test_worker_skips_custom_backend_jobs(
    conn: psycopg.Connection[DictRow], ticket: str, database_url: str
) -> None:
    conn.execute(
        "SELECT bee.add_column('ticket', 'geo', array['customer'], 'jsonb', p_backend => 'custom', p_model => 'geocoder')"
    )
    stats = await run_once(database_url, FakeProvider())
    assert stats.claimed == 0
    pending = conn.execute("SELECT count(*) AS n FROM bee.job WHERE status = 'pending'").fetchone()
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
        "SELECT bee.add_column('ticket', 'urgency', array['body'], 'enum', p_backend => 'decision',"
        " p_prompt => 'Urgenza', p_model => 'typesafe/jev-1.13',"
        ' p_output_schema => \'{"low": "routine", "high": "shop cannot sell"}\')'
    )
    provider = FakeProvider()
    stats = await run_once(database_url, provider)
    assert stats.outcomes == {"written": 3} and all(j.backend == "decision" for j in provider.calls)
    rows = conn.execute(
        "SELECT r.value, r.confidence, r.details FROM bee.result r WHERE r.is_current ORDER BY r.row_pk"
    ).fetchall()
    assert rows[0]["value"] == "high" and rows[0]["details"]["probabilities"]["high"] == 0.97
    assert conn.execute("SELECT urgency FROM ticket WHERE id = 1").fetchone() == {"urgency": "high"}


def test_decision_question_and_answer_mapping() -> None:
    from pgbee.providers import decision_answer, decision_question

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


def scalar(conn: psycopg.Connection[DictRow], sql: str) -> Any:
    row = conn.execute(sql).fetchone()
    assert row is not None
    return next(iter(row.values()))


def add_decision(conn: psycopg.Connection[DictRow], column: str, output_type: str) -> int:
    schema = '{"low": "routine", "high": "shop cannot sell"}' if output_type == "enum" else "{}"
    row = conn.execute(
        f"SELECT bee.add_column('ticket', '{column}', array['body'], '{output_type}',"
        " p_backend => 'decision', p_prompt => 'Question', p_model => 'typesafe/jev-1.13',"
        " p_output_schema => %s::jsonb) AS id",
        (schema,),
    ).fetchone()
    assert row is not None
    return int(row["id"])


async def test_decision_columns_of_the_same_row_share_one_call(
    conn: psycopg.Connection[DictRow], ticket: str, database_url: str
) -> None:
    add_decision(conn, "urgency", "enum")
    add_decision(conn, "blocking", "boolean")
    provider = FakeProvider()
    stats = await run_once(database_url, provider)
    assert stats.outcomes == {"written": 6}
    assert sorted(len(call) for call in provider.decide_calls) == [2, 2, 2]
    assert all(len({j.row_pk["id"] for j in call}) == 1 for call in provider.decide_calls)
    row = conn.execute("SELECT urgency, blocking FROM ticket WHERE id = 1").fetchone()
    assert row == {"urgency": "high", "blocking": True}
    shared = conn.execute(
        "SELECT DISTINCT (details ->> 'questions_in_call')::int AS n FROM bee.result"
    ).fetchall()
    assert shared == [{"n": 2}]


async def test_a_missing_answer_fails_only_its_own_job(
    conn: psycopg.Connection[DictRow], ticket: str, database_url: str
) -> None:
    add_decision(conn, "urgency", "enum")
    blocking = add_decision(conn, "blocking", "boolean")
    provider = FakeProvider()
    provider.unanswered_def = blocking
    stats = await run_once(database_url, provider)
    assert stats.outcomes == {"written": 3} and stats.failed == 3
    states = conn.execute(
        "SELECT column_def_id, status, last_error FROM bee.job ORDER BY column_def_id, id"
    ).fetchall()
    assert {(s["column_def_id"] == blocking, s["status"]) for s in states} == {
        (False, "done"),
        (True, "pending"),
    }


def test_fan_out_groups_by_row_and_model_and_caps_the_questions() -> None:
    from pgbee.worker import MAX_QUESTIONS_PER_CALL, fan_out

    def job(job_id: int, row: int, model: str = "typesafe/jev-1.13", body: str = "x") -> Job:
        return Job(
            job_id=job_id,
            column_def_id=job_id,
            column_version_id=job_id,
            backend="decision",
            model=model,
            prompt="q",
            output_type="boolean",
            output_schema={},
            backend_config={},
            config={},
            row_pk={"id": row},
            source_hash=b"h",
            source={"body": body},
            attempts=1,
        )

    calls = fan_out([job(1, 1), job(2, 1), job(3, 2), job(4, 1, model="other/model")])
    assert sorted(sorted(j.job_id for j in c) for c in calls) == [[1, 2], [3], [4]]
    many = fan_out([job(i, 1) for i in range(MAX_QUESTIONS_PER_CALL + 1)])
    assert [len(c) for c in many] == [MAX_QUESTIONS_PER_CALL, 1]


async def test_backfill_of_two_decision_columns_still_shares_calls(
    conn: psycopg.Connection[DictRow], ticket: str, database_url: str
) -> None:
    add_decision(conn, "urgency", "enum")  # its three jobs are queued first
    add_decision(conn, "blocking", "boolean")
    provider = FakeProvider()
    stats = await run_once(database_url, provider, batch_size=2)
    assert stats.claimed == 4, "two picked jobs bring their siblings on the same rows"
    assert sorted(len(call) for call in provider.decide_calls) == [2, 2]


def test_claim_brings_only_ready_decision_siblings_with_the_same_model(
    conn: psycopg.Connection[DictRow], ticket: str
) -> None:
    add_decision(conn, "urgency", "enum")
    same_model = add_decision(conn, "blocking", "boolean")
    conn.execute(
        "SELECT bee.add_column('ticket', 'spam', array['body'], 'boolean', 'decision',"
        " p_prompt => 'Spam?', p_model => 'typesafe/jev-2')"
    )
    conn.execute(
        "SELECT bee.add_column('ticket', 'summary', array['body'], 'text',"
        " p_prompt => 'Summary', p_model => 'openai/gpt-6-luna')"
    )
    # backfill order within a column is not row order: find the row claimed first
    first = scalar(conn, "SELECT row_pk ->> 'id' FROM bee.job ORDER BY next_attempt_at, id LIMIT 1")
    conn.execute(
        "UPDATE bee.job SET next_attempt_at = now() + interval '1 hour'"
        " WHERE column_def_id = %s AND row_pk ->> 'id' = %s",
        (same_model, first),
    )
    claimed = conn.execute(
        "SELECT d.column_name, j.row_pk ->> 'id' AS id FROM bee.claim_jobs('w', 1) j"
        " JOIN bee.column_def d ON d.id = j.column_def_id ORDER BY 1"
    ).fetchall()
    assert claimed == [{"column_name": "urgency", "id": first}], "sibling in backoff stays"
    claimed = conn.execute(
        "SELECT d.column_name, j.row_pk ->> 'id' AS id FROM bee.claim_jobs('w', 1) j"
        " JOIN bee.column_def d ON d.id = j.column_def_id ORDER BY 1"
    ).fetchall()
    assert [c["column_name"] for c in claimed] == ["blocking", "urgency"], (
        "the same model sibling comes along; other models and llm columns do not"
    )
    assert claimed[0]["id"] == claimed[1]["id"] != first


async def test_value_refused_by_the_database_fails_only_its_job(
    conn: psycopg.Connection[DictRow], ticket: str, database_url: str
) -> None:
    add_urgency(conn)
    # A rule of the user's own: the worker must not stop on it, nor retry it forever.
    conn.execute(
        "ALTER TABLE ticket ADD CONSTRAINT no_high CHECK (urgency IS DISTINCT FROM 'high')"
    )
    stats = await run_once(database_url, FakeProvider())
    assert stats.claimed == 3 and stats.failed == 1 and stats.outcomes == {"written": 2}
    dead = conn.execute("SELECT last_error FROM bee.dead_jobs").fetchall()
    assert len(dead) == 1 and "CheckViolation" in dead[0]["last_error"]
