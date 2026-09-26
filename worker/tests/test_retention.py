"""Lineage retention: old non current results and old done jobs go, current values stay."""

from __future__ import annotations

import psycopg
import pytest
from psycopg.rows import DictRow
from test_extension import add_urgency, claim, complete, scalar


def prune(conn: psycopg.Connection[DictRow], limit: int = 10000) -> dict[str, int]:
    row = conn.execute("SELECT results, jobs FROM bee.prune(%s)", (limit,)).fetchone()
    assert row is not None
    return dict(row)


def recompute_twice(conn: psycopg.Connection[DictRow]) -> None:
    """Three rows computed, then recomputed by a new version: three superseded results."""
    for job in claim(conn):
        complete(conn, job, "low")
    conn.execute("SELECT bee.update_column('ticket', 'urgency', p_prompt => 'v2')")
    for job in claim(conn):
        complete(conn, job, "medium")


def age(conn: psycopg.Connection[DictRow], days: int) -> None:
    conn.execute(
        "UPDATE bee.result SET created_at = now() - make_interval(days => %s) WHERE NOT is_current",
        (days,),
    )
    conn.execute(
        "UPDATE bee.job SET updated_at = now() - make_interval(days => %s) WHERE status = 'done'",
        (days,),
    )


def test_without_retention_everything_is_kept(
    conn: psycopg.Connection[DictRow], ticket: str
) -> None:
    add_urgency(conn)
    recompute_twice(conn)
    age(conn, 400)
    assert prune(conn) == {"results": 0, "jobs": 6}, "done jobs go after a week anyway"
    assert scalar(conn, "SELECT count(*) FROM bee.result") == 6


def test_retention_deletes_only_old_superseded_results(
    conn: psycopg.Connection[DictRow], ticket: str
) -> None:
    add_urgency(conn, lineage_retention_days=30)
    recompute_twice(conn)
    conn.execute("UPDATE ticket SET urgency = 'high' WHERE id = 1")  # human, supersedes a model
    age(conn, 5)
    assert prune(conn) == {"results": 0, "jobs": 0}, "younger than both retentions"
    age(conn, 31)
    spent_before = scalar(conn, "SELECT sum(cost) FROM bee.spend")
    assert prune(conn) == {"results": 4, "jobs": 6}
    current = conn.execute(
        "SELECT source, count(*) AS n FROM bee.result GROUP BY source ORDER BY source"
    ).fetchall()
    assert current == [{"source": "model", "n": 2}, {"source": "human", "n": 1}]
    assert scalar(conn, "SELECT count(*) FROM bee.result WHERE NOT is_current") == 0
    assert scalar(conn, "SELECT sum(cost) FROM bee.spend") == spent_before, "spend is kept"
    assert scalar(conn, "SELECT urgency FROM ticket WHERE id = 1") == "high"


def test_prune_works_in_bounded_batches(conn: psycopg.Connection[DictRow], ticket: str) -> None:
    add_urgency(conn, lineage_retention_days=1)
    recompute_twice(conn)
    age(conn, 10)
    assert prune(conn, limit=2) == {"results": 2, "jobs": 2}
    assert prune(conn, limit=2) == {"results": 1, "jobs": 2}
    assert prune(conn, limit=2) == {"results": 0, "jobs": 2}


def test_dead_jobs_are_kept(conn: psycopg.Connection[DictRow], ticket: str) -> None:
    add_urgency(conn)
    for job in claim(conn):
        conn.execute("SELECT bee.fail_job(%s::bigint, 'boom', false)", (job["job_id"],))
    conn.execute("UPDATE bee.job SET updated_at = now() - interval '30 days'")
    assert prune(conn) == {"results": 0, "jobs": 0}


def test_retention_is_validated(conn: psycopg.Connection[DictRow], ticket: str) -> None:
    bad: object
    for bad in (0, 1.5, "30"):
        with pytest.raises(psycopg.errors.RaiseException, match="lineage_retention_days"):
            add_urgency(conn, lineage_retention_days=bad)
