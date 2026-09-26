"""Smoke tests against OpenRouter for real (marker llm, excluded by default, cost < 0.001 USD).

Run with `make test-llm`. They check what the fake provider cannot: the request shapes each
backend sends, the answers the real models return, errors as the provider really raises them.
"""

from __future__ import annotations

import json

import psycopg
import pytest
from psycopg.rows import DictRow

from aicol.db import Contract
from aicol.providers import OpenRouterProvider
from aicol.settings import load_settings
from aicol.worker import Worker

pytestmark = pytest.mark.llm

LLM_MODEL = "openai/gpt-6-luna"
DECISION_MODEL = "typesafe/jev-1.13"
EMBEDDING_MODEL = "openai/text-embedding-3-small"


@pytest.fixture
def provider() -> OpenRouterProvider:
    settings = load_settings()
    if not settings.openrouter_api_key:
        pytest.skip("OPENROUTER_API_KEY is not set")
    return OpenRouterProvider(settings.openrouter_api_key, settings.openrouter_base_url)


async def drain(database_url: str, provider: OpenRouterProvider) -> None:
    contract = await Contract.connect(database_url)
    try:
        worker = Worker(contract, provider, worker_id="smoke", batch_size=50)
        await worker.run_once()
    finally:
        await contract.close()


async def test_every_backend_fills_its_column_through_openrouter(
    database_url: str,
    conn: psycopg.Connection[DictRow],
    ticket: str,
    provider: OpenRouterProvider,
) -> None:
    conn.execute(
        "SELECT ai.add_column('ticket', 'urgency', array['body'], 'enum',"
        " p_prompt => 'How urgent is this support ticket?', p_model => %s,"
        " p_output_schema => %s::jsonb, p_backend_config => %s::jsonb)",
        (
            LLM_MODEL,
            json.dumps(["low", "medium", "high"]),
            json.dumps({"reasoning": {"effort": "low"}}),
        ),
    )
    conn.execute(
        "SELECT ai.add_column('ticket', 'blocking', array['body'], 'boolean', 'decision',"
        " p_prompt => 'Is the customer unable to sell or get paid right now?', p_model => %s)",
        (DECISION_MODEL,),
    )
    conn.execute(
        "SELECT ai.add_column('ticket', 'topic', array['body'], 'enum', 'decision',"
        " p_prompt => 'What is the ticket about?', p_model => %s, p_output_schema => %s::jsonb)",
        (
            DECISION_MODEL,
            json.dumps(
                {
                    "technical": "the site, the app or an integration misbehaves",
                    "billing": "invoices, payments, refunds, billing details",
                    "other": "anything else",
                }
            ),
        ),
    )
    conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
    conn.execute(
        "SELECT ai.add_column('ticket', 'embedding', array['body'], 'vector', 'embedding',"
        " p_model => %s, p_output_schema => '{\"dimensions\": 16}',"
        " p_backend_config => '{\"dimensions\": 16}')",
        (EMBEDDING_MODEL,),
    )
    await drain(database_url, provider)

    jobs = conn.execute(
        "SELECT status, count(*) AS n, max(last_error) AS error FROM ai.job GROUP BY status"
    ).fetchall()
    assert jobs == [{"status": "done", "n": 12, "error": None}]

    rows = conn.execute(
        "SELECT id, urgency, blocking, topic, embedding FROM ticket ORDER BY id"
    ).fetchall()
    assert [r["topic"] for r in rows[:2]] == ["technical", "billing"]
    assert rows[0]["urgency"] == "high" and rows[0]["blocking"] is True
    assert all(r["urgency"] in ("low", "medium", "high") for r in rows)
    assert rows[1]["blocking"] is False and rows[2]["blocking"] is False
    assert all(r["embedding"] is not None for r in rows)

    decision = conn.execute(
        "SELECT r.confidence, r.details FROM ai.result r JOIN ai.column_def d"
        " ON d.id = r.column_def_id WHERE d.column_name = 'blocking' AND r.row_pk = '{\"id\": 1}'"
    ).fetchone()
    assert decision is not None
    assert 0.5 <= decision["confidence"] <= 1
    assert 0.5 <= decision["details"]["probability_true"] <= 1
    assert decision["details"]["questions_in_call"] == 2, "blocking and topic share the call"

    costs = conn.execute("SELECT column_name, cost FROM ai.cost_by_column").fetchall()
    assert {c["column_name"] for c in costs} == {"urgency", "blocking", "topic", "embedding"}
    assert all(c["cost"] is not None and c["cost"] > 0 for c in costs)


async def test_unknown_model_kills_the_job_without_retrying(
    database_url: str,
    conn: psycopg.Connection[DictRow],
    ticket: str,
    provider: OpenRouterProvider,
) -> None:
    conn.execute(
        "SELECT ai.add_column('ticket', 'urgency', array['body'], 'enum',"
        " p_prompt => 'How urgent is this?', p_model => 'nobody/no-such-model',"
        ' p_output_schema => \'["low", "high"]\')'
    )
    await drain(database_url, provider)
    jobs = conn.execute("SELECT status, attempts, last_error FROM ai.job").fetchall()
    assert len(jobs) == 3
    assert all(j["status"] == "dead" and j["attempts"] == 1 for j in jobs)
    assert all(j["last_error"].startswith("provider 4") for j in jobs)
