"""Incremental backfill: one chunk at declaration, the rest driven by claim_jobs."""

from __future__ import annotations

import psycopg
import pytest
from psycopg.rows import DictRow
from test_extension import add_urgency, claim, complete, scalar


def backfill_state(conn: psycopg.Connection[DictRow]) -> dict[str, object]:
    row = conn.execute(
        "SELECT backfill_pending, backfill_scanned FROM ai.columns WHERE column_name = 'urgency'"
    ).fetchone()
    assert row is not None
    return dict(row)


def queued_rows(conn: psycopg.Connection[DictRow]) -> list[int]:
    rows = conn.execute("SELECT (row_pk ->> 'id')::int AS id FROM ai.job ORDER BY 1").fetchall()
    return [r["id"] for r in rows]


@pytest.fixture
def five_tickets(conn: psycopg.Connection[DictRow], ticket: str) -> str:
    conn.execute("INSERT INTO ticket (body) VALUES ('quarto'), ('quinto')")
    return ticket


def test_declaration_enqueues_one_chunk_and_claims_drive_the_rest(
    conn: psycopg.Connection[DictRow], five_tickets: str
) -> None:
    add_urgency(conn, backfill_chunk=2)
    assert queued_rows(conn) == [1, 2]
    assert backfill_state(conn) == {"backfill_pending": True, "backfill_scanned": 2}

    first = claim(conn, 2)
    assert [j["row_pk"]["id"] for j in first] == [1, 2], "queue full before: no chunk added"
    assert queued_rows(conn) == [1, 2, 3, 4], "after taking, the claim refills the queue"

    claim(conn, 2)
    assert queued_rows(conn) == [1, 2, 3, 4, 5]
    assert backfill_state(conn) == {"backfill_pending": False, "backfill_scanned": 5}


def test_rows_with_nothing_to_do_are_scanned_past(
    conn: psycopg.Connection[DictRow], five_tickets: str
) -> None:
    add_urgency(conn, backfill_chunk=2)
    while jobs := claim(conn):
        for job in jobs:
            complete(conn, job, "low")
    assert backfill_state(conn)["backfill_pending"] is False
    conn.execute("UPDATE ticket SET body = 'cambiato' WHERE id = 5")  # trigger enqueues it
    conn.execute("DELETE FROM ai.job WHERE status = 'pending'")  # as if lost: backfill finds it

    assert scalar(conn, "SELECT ai.backfill(id) FROM ai.column_def") == 0, "rows 1-2 are current"
    assert claim(conn, 10) != [], "one claim scans up to ten chunks to fill the queue"
    assert queued_rows(conn)[-1] == 5
    assert backfill_state(conn) == {"backfill_pending": False, "backfill_scanned": 5}


def test_backfill_walks_composite_and_uuid_keys(conn: psycopg.Connection[DictRow]) -> None:
    conn.execute(
        "CREATE TABLE note (tenant uuid NOT NULL, seq integer NOT NULL, body text NOT NULL,"
        " PRIMARY KEY (tenant, seq))"
    )
    conn.execute(
        "INSERT INTO note (tenant, seq, body)"
        " SELECT t, s, 'nota ' || s FROM (VALUES (gen_random_uuid()), (gen_random_uuid())) u (t),"
        " generate_series(1, 3) s"
    )
    conn.execute(
        "SELECT ai.add_column('note', 'kind', array['body'], 'text', p_prompt => 'p',"
        " p_model => 'm', p_config => '{\"backfill_chunk\": 1}')"
    )
    seen: list[tuple[str, int]] = []
    while jobs := claim(conn, 1):
        seen.extend((j["row_pk"]["tenant"], j["row_pk"]["seq"]) for j in jobs)
        for job in jobs:
            complete(conn, job, "appunto")
    assert len(seen) == 6 and len(set(seen)) == 6
    assert scalar(conn, "SELECT count(*) FROM note WHERE kind IS NULL") == 0


def test_update_column_rescans_in_chunks_and_skips_pinned_rows(
    conn: psycopg.Connection[DictRow], five_tickets: str
) -> None:
    add_urgency(conn, backfill_chunk=2)
    while jobs := claim(conn):
        for job in jobs:
            complete(conn, job, "low")
    conn.execute("UPDATE ticket SET urgency = 'high' WHERE id = 4")  # human pin
    conn.execute("SELECT ai.update_column('ticket', 'urgency', p_prompt => 'v2')")
    recomputed: list[int] = []
    while jobs := claim(conn):
        recomputed.extend(j["row_pk"]["id"] for j in jobs)
        for job in jobs:
            complete(conn, job, "medium")
    assert sorted(recomputed) == [1, 2, 3, 5]
    assert scalar(conn, "SELECT urgency FROM ticket WHERE id = 4") == "high"


def test_disabled_or_over_budget_columns_do_not_advance(
    conn: psycopg.Connection[DictRow], five_tickets: str
) -> None:
    add_urgency(conn, backfill_chunk=2, budget_usd=0)
    conn.execute("DELETE FROM ai.job")
    assert claim(conn) == []
    assert backfill_state(conn)["backfill_scanned"] == 2
    conn.execute("SELECT ai.configure('ticket', 'urgency', '{\"budget_usd\": null}')")
    conn.execute("SELECT ai.disable('ticket', 'urgency')")
    assert claim(conn) == []
    assert backfill_state(conn)["backfill_scanned"] == 2


def test_backfill_chunk_is_validated(conn: psycopg.Connection[DictRow], ticket: str) -> None:
    bad: object
    for bad in (0, 1.5, "10", 200000):
        with pytest.raises(psycopg.errors.RaiseException, match="backfill_chunk"):
            add_urgency(conn, backfill_chunk=bad)
