"""Least privilege: a worker login in bee_worker and an application role that only writes the
user table. Roles are cluster wide, so they are created once and dropped at the end."""

from __future__ import annotations

import json
from collections.abc import Iterator

import psycopg
import pytest
from fakes import FakeProvider, add_urgency
from psycopg.rows import DictRow, dict_row

from pgbee.db import Contract
from pgbee.worker import Worker

WORKER_ROLE = "pgbee_test_worker"
APP_ROLE = "pgbee_test_app"
PASSWORD = "Pgbee-Test-Only-7fK3q9Lx"  # strong: managed services (Neon) reject weak ones
CONTRACT = [
    "bee.claim_jobs(text, integer, bee.backend[])",
    "bee.complete_job(bigint, bytea, jsonb, real, text, jsonb, integer, jsonb)",
    "bee.fail_job(bigint, text, boolean)",
    "bee.reclaim_stale(interval)",
    "bee.prune(integer, interval)",
]
DEFINER = [*CONTRACT, "bee.enqueue_trigger()", "bee.override_trigger()"]


def url_as(database_url: str, role: str) -> str:
    scheme, _, rest = database_url.partition("://")
    host = rest.partition("@")[2]
    return f"{scheme}://{role}:{PASSWORD}@{host}"


@pytest.fixture(scope="module")
def roles(database_url: str) -> Iterator[None]:
    with psycopg.connect(database_url, autocommit=True) as admin:
        for role, extra in ((WORKER_ROLE, " IN ROLE bee_worker"), (APP_ROLE, "")):
            drop_role(admin, role)
            admin.execute(f"CREATE ROLE {role} LOGIN PASSWORD '{PASSWORD}'{extra}")
    yield
    with psycopg.connect(database_url, autocommit=True) as admin:
        for role in (WORKER_ROLE, APP_ROLE):
            drop_role(admin, role)


def drop_role(admin: psycopg.Connection[tuple[object, ...]], role: str) -> None:
    """Without superuser (managed services), DROP OWNED needs the privileges of the role: the
    creator has ADMIN on it since Postgres 16 and grants it to itself first."""
    if admin.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone() is None:
        return
    admin.execute(f"GRANT {role} TO CURRENT_USER")
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
        assert w.execute("SELECT count(*) AS n FROM bee.columns").fetchone() == {"n": 1}
        for sql in (
            "SELECT * FROM ticket",
            "UPDATE bee.job SET status = 'done'",
            "SELECT bee.add_column('ticket', 'x', array['body'], 'text', p_prompt => 'p',"
            " p_model => 'm')",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                w.execute(sql)
            w.rollback()


def test_application_role_writes_enqueue_and_override(
    database_url: str, conn: psycopg.Connection[DictRow], app_ticket: str
) -> None:
    add_urgency(conn)
    conn.execute("DELETE FROM bee.job")
    with psycopg.connect(url_as(database_url, APP_ROLE), autocommit=True) as app:
        app.execute("INSERT INTO ticket (body) VALUES ('Urgente: il sito è giù')")
        app.execute("UPDATE ticket SET urgency = 'low' WHERE id = 2")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            app.execute("SELECT * FROM bee.claim_jobs('intruder', 10)")
    queued = conn.execute("SELECT row_pk FROM bee.job").fetchall()
    assert queued == [{"row_pk": {"id": 4}}]
    human = conn.execute(
        "SELECT value FROM bee.result WHERE source = 'human' AND row_pk = %s::jsonb",
        (json.dumps({"id": 2}),),
    ).fetchone()
    assert human == {"value": "low"}


async def test_worker_role_writes_vectors(
    database_url: str, conn: psycopg.Connection[DictRow], app_ticket: str
) -> None:
    conn.execute(
        "SELECT bee.add_column('ticket', 'embedding', array['body'], 'vector', 'embedding',"
        " p_model => 'm', p_output_schema => '{\"dimensions\": 3}')"
    )
    contract = await Contract.connect(url_as(database_url, WORKER_ROLE))
    try:
        stats = await Worker(contract, FakeProvider(), worker_id="restricted").run_once()
    finally:
        await contract.close()
    assert stats.outcomes == {"written": 3}
