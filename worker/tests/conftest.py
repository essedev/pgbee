"""Integration fixtures: a dedicated database, recreated per session, with the extension applied."""

from __future__ import annotations

import os
from collections.abc import Iterator

import psycopg
import pytest
from psycopg.rows import DictRow, dict_row

from pgbee.installer import install

ADMIN_URL = os.environ.get("DATABASE_URL", "postgresql://aidb:aidb@localhost:4460/aidb")
TEST_DB = "aidb_test"


def _test_url() -> str:
    base, _, _ = ADMIN_URL.rpartition("/")
    return f"{base}/{TEST_DB}"


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    with psycopg.connect(ADMIN_URL, autocommit=True) as admin:
        admin.execute(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")
        admin.execute(f"CREATE DATABASE {TEST_DB}")
    url = _test_url()
    with psycopg.connect(url) as conn:
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        conn.commit()
        install(conn)
        conn.commit()
    yield url


@pytest.fixture
def conn(database_url: str) -> Iterator[psycopg.Connection[DictRow]]:
    """Autocommit connection on a clean slate: user tables and bee rows removed after each test."""
    with psycopg.connect(database_url, autocommit=True, row_factory=dict_row) as c:
        yield c
        _reset(c)


@pytest.fixture
def conn2(database_url: str) -> Iterator[psycopg.Connection[DictRow]]:
    """A second connection for concurrency tests."""
    with psycopg.connect(database_url, autocommit=True, row_factory=dict_row) as c:
        yield c


def _reset(c: psycopg.Connection[DictRow]) -> None:
    tables = c.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'").fetchall()
    for t in tables:
        c.execute(f'DROP TABLE IF EXISTS public."{t["tablename"]}" CASCADE')
    c.execute("DELETE FROM bee.job")
    c.execute("DELETE FROM bee.spend")
    c.execute("DELETE FROM bee.result")
    c.execute("UPDATE bee.column_def SET current_version_id = NULL")
    c.execute("DELETE FROM bee.column_version")
    c.execute("DELETE FROM bee.column_def")


@pytest.fixture
def ticket(conn: psycopg.Connection[DictRow]) -> str:
    """A support ticket table with three rows, no derived column yet."""
    conn.execute(
        "CREATE TABLE ticket (id bigserial PRIMARY KEY, body text NOT NULL,"
        " customer text, created_at timestamptz DEFAULT now())"
    )
    conn.execute(
        "INSERT INTO ticket (body, customer) VALUES"
        " ('Il sito è giù da un''ora, nessuno riesce a pagare', 'Acme'),"
        " ('Vorrei cambiare l''indirizzo di fatturazione', 'Beta'),"
        " ('Come esporto i dati in CSV?', 'Gamma')"
    )
    return "ticket"
