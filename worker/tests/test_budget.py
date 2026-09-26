"""Spending cap per derived column: counted from reported usage, enforced by claim_jobs."""

from __future__ import annotations

import json
from decimal import Decimal

import psycopg
import pytest
from psycopg.rows import DictRow
from test_extension import add_urgency, claim, complete, scalar


def budget_row(conn: psycopg.Connection[DictRow]) -> dict[str, object]:
    row = conn.execute(
        "SELECT budget_usd, budget_period, spent_usd, spent_total_usd, remaining_usd, exhausted"
        " FROM bee.budgets WHERE column_name = 'urgency'"
    ).fetchone()
    assert row is not None
    return dict(row)


def test_budget_config_is_validated(conn: psycopg.Connection[DictRow], ticket: str) -> None:
    with pytest.raises(psycopg.errors.RaiseException, match="budget_usd"):
        add_urgency(conn, budget_usd="10")
    with pytest.raises(psycopg.errors.RaiseException, match="budget_usd"):
        add_urgency(conn, budget_usd=-1)
    add_urgency(conn)
    with pytest.raises(psycopg.errors.RaiseException, match="budget_period"):
        conn.execute("SELECT bee.configure('ticket', 'urgency', '{\"budget_period\": \"week\"}')")
    conn.execute("SELECT bee.configure('ticket', 'urgency', '{\"budget_usd\": 0.5}')")
    conn.execute("SELECT bee.configure('ticket', 'urgency', '{\"budget_usd\": null}')")


def test_spend_is_counted_from_every_model_result(
    conn: psycopg.Connection[DictRow], ticket: str
) -> None:
    def_id = add_urgency(conn)
    for job in claim(conn):
        complete(conn, job, "low")  # each reports cost 0.0001
    conn.execute("UPDATE ticket SET urgency = 'high' WHERE id = 1")  # human result, no cost
    assert scalar(conn, f"SELECT bee.spent({def_id})") == Decimal("0.0003")
    assert scalar(conn, "SELECT results FROM bee.spend") == 3
    assert budget_row(conn) == {
        "budget_usd": None,
        "budget_period": "month",
        "spent_usd": Decimal("0.0003"),
        "spent_total_usd": Decimal("0.0003"),
        "remaining_usd": None,
        "exhausted": False,
    }


def test_claim_stops_at_the_budget_and_resumes_when_raised(
    conn: psycopg.Connection[DictRow], conn2: psycopg.Connection[DictRow], ticket: str
) -> None:
    add_urgency(conn, budget_usd=0.0002)
    first = claim(conn, 2)
    for job in first:
        complete(conn, job, "low")
    assert budget_row(conn)["exhausted"] is True
    assert claim(conn) == [], "budget used up: the third row waits"
    assert scalar(conn, "SELECT count(*) FROM bee.job WHERE status = 'pending'") == 1

    conn2.execute("LISTEN bee_jobs")
    conn.execute("SELECT bee.configure('ticket', 'urgency', '{\"budget_usd\": 0.001}')")
    assert len(list(conn2.notifies(timeout=1, stop_after=1))) == 1, "raising it wakes the workers"
    assert len(claim(conn)) == 1


def test_budget_period_only_counts_the_current_window(
    conn: psycopg.Connection[DictRow], ticket: str
) -> None:
    def_id = add_urgency(conn, budget_usd=1, budget_period="day")
    conn.execute(
        "INSERT INTO bee.spend (column_def_id, day, cost, results)"
        " VALUES (%s, (now() AT TIME ZONE 'UTC')::date - 40, 5, 100)",
        (def_id,),
    )
    assert len(claim(conn, 1)) == 1, "older spend does not count for a daily cap"
    for period, spent in (("day", 0), ("month", 0), ("total", 5)):
        assert scalar(conn, f"SELECT bee.spent({def_id}, '{period}')") == spent
    conn.execute(
        "SELECT bee.configure('ticket', 'urgency', %s::jsonb)",
        (json.dumps({"budget_period": "total"}),),
    )
    assert claim(conn) == []


def test_budget_blocks_only_its_own_column(conn: psycopg.Connection[DictRow], ticket: str) -> None:
    add_urgency(conn, budget_usd=0)
    conn.execute(
        "SELECT bee.add_column('ticket', 'lang', array['body'], 'text',"
        " p_prompt => 'Language', p_model => 'openai/gpt-6-luna')"
    )
    claimed = claim(conn)
    assert {j["column_def_id"] for j in claimed} == {
        scalar(conn, "SELECT id FROM bee.column_def WHERE column_name = 'lang'")
    }
