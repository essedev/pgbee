"""Least privilege: a worker login in ai_worker and an application role that only writes the
user table. Roles are cluster wide, so they are created once and dropped at the end."""

from __future__ import annotations

import json
from collections.abc import Iterator

import psycopg
import pytest
from fakes import FakeProvider, add_urgency
from psycopg.rows import DictRow, dict_row

from aicol.db import Contract
from aicol.worker import Worker

WORKER_ROLE = "aicol_test_worker"
APP_ROLE = "aicol_test_app"
PASSWORD = "test-only"
CONTRACT = [
    "ai.claim_jobs(text, integer, ai.backend[])",
    "ai.complete_job(bigint, bytea, jsonb, real, text, jsonb, integer, jsonb)",
    "ai.fail_job(bigint, text, boolean)",
    "ai.reclaim_stale(interval)",
    "ai.prune(integer, interval)",
]
DEFINER = [*CONTRACT, "ai.enqueue_trigger()", "ai.override_trigger()"]


def url_as(database_url: str, role: str) -> str:
    scheme, _, rest = database_url.partition("://")
    host = rest.partition("@")[2]
    return f"{scheme}://{role}:{PASSWORD}@{host}"


@pytest.fixture(scope="module")
def roles(database_url: str) -> Iterator[None]:
    with psycopg.connect(database_url, autocommit=True) as admin:
        for role, extra in ((WORKER_ROLE, " IN ROLE ai_worker"), (APP_ROLE, "")):
            admin.execute(f"DROP ROLE IF EXISTS {role}")
            admin.execute(f"CREATE ROLE {role} LOGIN PASSWORD '{PASSWORD}'{extra}")
    yield
    with psycopg.connect(database_url, autocommit=True) as admin:
        for role in (WORKER_ROLE, APP_ROLE):
            admin.execute(f"DROP OWNED BY {role}")
            admin.execute(f"DROP ROLE {role}")


@pytest.fixture
def app_ticket(conn: psycopg.Connection[DictRow], ticket: str, roles: None) -> str:
    conn.execute(f"GRANT SELECT, INSERT, UPDATE ON ticket TO {APP_ROLE}")
    conn.execute(f"GRANT USAGE ON SEQUENCE ticket_id_seq TO {APP_ROLE}")
    return ticket


def test_contract_runs_as_owner_and_is_not_public(conn: psycopg.Connection[DictRow]) -> None:
    for signature in DEFINER:
        row = conn.execute(
            "SELECT p.prosecdef, p.proconfig FROM pg_proc p WHERE p.oid = %s::regprocedure",
            (signature,),
        ).fetchone()
        assert row is not None
        assert row["prosecdef"], f"{signature} lost SECURITY DEFINER (CREATE OR REPLACE?)"
        assert row["proconfig"] == ["search_path=pg_catalog, pg_temp"], signature
    for signature in CONTRACT:
        public = conn.execute(
            "SELECT has_function_privilege('public', %s, 'EXECUTE') AS ok", (signature,)
        ).fetchone()
        assert public == {"ok": False}, f"{signature} is executable by PUBLIC"


async def test_worker_role_fills_columns_without_table_access(
    database_url: str, conn: psycopg.Connection[DictRow], app_ticket: str
) -> None:
    add_urgency(conn)
    contract = await Contract.connect(url_as(database_url, WORKER_ROLE))
    try:
        worker = Worker(contract, FakeProvider(), worker_id="restricted")
        stats = await worker.run_once()
    finally:
        await contract.close()
    assert stats.outcomes == {"written": 3}
    assert conn.execute("SELECT urgency FROM ticket WHERE id = 1").fetchone() == {"urgency": "high"}

    with psycopg.connect(url_as(database_url, WORKER_ROLE), row_factory=dict_row) as w:
        assert w.execute("SELECT count(*) AS n FROM ai.columns").fetchone() == {"n": 1}
        for sql in (
            "SELECT * FROM ticket",
            "UPDATE ai.job SET status = 'done'",
            "SELECT ai.add_column('ticket', 'x', array['body'], 'text', p_prompt => 'p',"
            " p_model => 'm')",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                w.execute(sql)
            w.rollback()


def test_application_role_writes_enqueue_and_override(
    database_url: str, conn: psycopg.Connection[DictRow], app_ticket: str
) -> None:
    add_urgency(conn)
    conn.execute("DELETE FROM ai.job")
    with psycopg.connect(url_as(database_url, APP_ROLE), autocommit=True) as app:
        app.execute("INSERT INTO ticket (body) VALUES ('Urgente: il sito è giù')")
        app.execute("UPDATE ticket SET urgency = 'low' WHERE id = 2")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            app.execute("SELECT * FROM ai.claim_jobs('intruder', 10)")
    queued = conn.execute("SELECT row_pk FROM ai.job").fetchall()
    assert queued == [{"row_pk": {"id": 4}}]
    human = conn.execute(
        "SELECT value FROM ai.result WHERE source = 'human' AND row_pk = %s::jsonb",
        (json.dumps({"id": 2}),),
    ).fetchone()
    assert human == {"value": "low"}


async def test_worker_role_writes_vectors(
    database_url: str, conn: psycopg.Connection[DictRow], app_ticket: str
) -> None:
    conn.execute(
        "SELECT ai.add_column('ticket', 'embedding', array['body'], 'vector', 'embedding',"
        " p_model => 'm', p_output_schema => '{\"dimensions\": 3}')"
    )
    contract = await Contract.connect(url_as(database_url, WORKER_ROLE))
    try:
        stats = await Worker(contract, FakeProvider(), worker_id="restricted").run_once()
    finally:
        await contract.close()
    assert stats.outcomes == {"written": 3}
