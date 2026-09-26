"""Packaging: the files for CREATE EXTENSION aicol, and the extension itself on a Postgres that
has them (marker `extension`, run by `make test-extension`, which builds the image and starts
it on port 4463 as the container aicol-ext-test)."""

from __future__ import annotations

import os
import subprocess
import uuid
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from fakes import FakeProvider
from psycopg.rows import DictRow, dict_row

from aicol import extension
from aicol.db import Contract
from aicol.installer import InstalledAsExtension, install, sql_files
from aicol.worker import Worker

EXT_URL = os.environ.get("AICOL_EXTENSION_URL", "postgresql://aidb:aidb@localhost:4463/aidb")
CONTAINER = os.environ.get("AICOL_EXTENSION_CONTAINER", "aicol-ext-test")


def test_extension_files_chain_every_sql_file(tmp_path: Path) -> None:
    written = extension.build(tmp_path)
    files = sql_files()
    last = files[-1].version
    names = sorted(p.name for p in written)
    expected = ["aicol--0.1.sql", "aicol.control"] + [
        f"aicol--0.{a.version}--0.{b.version}.sql" for a, b in zip(files, files[1:], strict=False)
    ]
    assert names == sorted(expected)
    assert f"default_version = '0.{last}'" in (tmp_path / "aicol.control").read_text()
    for path in written:
        if path.suffix == ".sql":
            text = path.read_text()
            assert text.startswith("\\echo Use")
            assert "pg_extension_config_dump" in text
            assert "BEGIN;" not in text and "COMMIT;" not in text


@pytest.fixture
def ext_db() -> Iterator[str]:
    """A fresh database on the extension server, dropped afterwards."""
    name = f"ext_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(EXT_URL, autocommit=True) as admin:
        admin.execute(f"CREATE DATABASE {name}")
    base, _, _ = EXT_URL.rpartition("/")
    yield f"{base}/{name}"
    with psycopg.connect(EXT_URL, autocommit=True) as admin:
        admin.execute(f"DROP DATABASE {name} WITH (FORCE)")


def functions(conn: psycopg.Connection[DictRow]) -> list[tuple[str, bool]]:
    rows = conn.execute(
        "SELECT p.oid::regprocedure::text AS sig, p.prosecdef FROM pg_proc p"
        " JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'ai' ORDER BY 1"
    ).fetchall()
    return [(r["sig"], r["prosecdef"]) for r in rows]


@pytest.mark.extension
async def test_create_extension_runs_the_whole_cycle(ext_db: str) -> None:
    with psycopg.connect(ext_db, autocommit=True, row_factory=dict_row) as conn:
        conn.execute("CREATE EXTENSION aicol")
        conn.execute("CREATE TABLE ticket (id serial PRIMARY KEY, body text NOT NULL)")
        conn.execute("INSERT INTO ticket (body) VALUES ('Il sito è giù'), ('Fattura')")
        conn.execute(
            "SELECT ai.add_column('ticket', 'urgency', array['body'], 'enum', p_prompt => 'u',"
            " p_model => 'm', p_output_schema => '[\"low\", \"high\"]')"
        )
        contract = await Contract.connect(ext_db)
        try:
            stats = await Worker(contract, FakeProvider(), worker_id="ext").run_once()
        finally:
            await contract.close()
        assert stats.outcomes == {"written": 2}
        with pytest.raises(InstalledAsExtension):
            install(conn)


@pytest.mark.extension
def test_updating_from_the_first_version_equals_a_fresh_install(ext_db: str) -> None:
    with psycopg.connect(ext_db, autocommit=True, row_factory=dict_row) as conn:
        conn.execute("CREATE EXTENSION aicol")
        fresh = functions(conn)
        conn.execute("DROP EXTENSION aicol CASCADE")
        conn.execute("DROP SCHEMA IF EXISTS ai CASCADE")
        conn.execute("CREATE EXTENSION aicol VERSION '0.1'")
        conn.execute("ALTER EXTENSION aicol UPDATE")
        assert functions(conn) == fresh
        version = conn.execute(
            "SELECT extversion FROM pg_extension WHERE extname = 'aicol'"
        ).fetchone()
        assert version == {"extversion": f"0.{sql_files()[-1].version}"}


@pytest.mark.extension
def test_dump_and_restore_keep_definitions_lineage_and_spend(ext_db: str) -> None:
    name = ext_db.rpartition("/")[2]
    with psycopg.connect(ext_db, autocommit=True, row_factory=dict_row) as conn:
        conn.execute("CREATE EXTENSION aicol")
        conn.execute("CREATE TABLE ticket (id serial PRIMARY KEY, body text NOT NULL)")
        conn.execute("INSERT INTO ticket (body) VALUES ('a'), ('b')")
        conn.execute(
            "SELECT ai.add_column('ticket', 'urgency', array['body'], 'enum', p_prompt => 'u',"
            " p_model => 'm', p_output_schema => '[\"low\", \"high\"]')"
        )
        conn.execute(
            "SELECT ai.complete_job(job_id, source_hash, '\"low\"', 0.9, 'm', '{\"cost\": 0.01}')"
            " FROM ai.claim_jobs('w', 1)"
        )
        conn.execute("UPDATE ticket SET urgency = 'high' WHERE id = 2")
    restored = f"{name}_restored"
    for cmd in (
        ["pg_dump", "-U", "aidb", "-Fc", "-f", f"/tmp/{name}.dump", name],
        ["createdb", "-U", "aidb", restored],
        ["pg_restore", "-U", "aidb", "-d", restored, "--exit-on-error", f"/tmp/{name}.dump"],
    ):
        subprocess.run(["docker", "exec", CONTAINER, *cmd], check=True, capture_output=True)
    base = ext_db.rpartition("/")[0]
    try:
        with psycopg.connect(f"{base}/{restored}", autocommit=True, row_factory=dict_row) as r:
            counts = r.execute(
                "SELECT (SELECT count(*) FROM ai.column_def) AS defs,"
                " (SELECT count(*) FROM ai.result) AS results,"
                " (SELECT sum(cost) FROM ai.spend)::float AS spend"
            ).fetchone()
            assert counts == {"defs": 1, "results": 2, "spend": 0.01}
            r.execute("INSERT INTO ticket (body) VALUES ('c')")
            claimed = r.execute("SELECT count(*) AS n FROM ai.claim_jobs('w', 10)").fetchone()
            assert claimed == {"n": 1}, "triggers and queue work after the restore"
    finally:
        with psycopg.connect(EXT_URL, autocommit=True) as admin:
            admin.execute(f"DROP DATABASE IF EXISTS {restored} WITH (FORCE)")
