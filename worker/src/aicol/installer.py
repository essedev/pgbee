"""Apply the versioned SQL files in `sql/` to a database and track them in ai.schema_version."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import tuple_row

SQL_FILE_RE = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


class InstalledAsExtension(RuntimeError):
    """The database has ai-db as a Postgres extension; the file installer must not touch it."""


@dataclass(frozen=True)
class SqlFile:
    version: int
    path: Path


def sql_dir() -> Path:
    """Directory holding the extension SQL files, shipped inside the package.

    Overridable with AICOL_SQL_DIR. The repository's top level `sql` links here.
    """
    env = os.environ.get("AICOL_SQL_DIR")
    if env:
        return Path(env)
    return Path(__file__).resolve().parent / "sql"


def sql_files(directory: Path | None = None) -> list[SqlFile]:
    directory = directory or sql_dir()
    files: list[SqlFile] = []
    for path in sorted(directory.glob("*.sql")):
        match = SQL_FILE_RE.match(path.name)
        if not match:
            raise ValueError(f"unexpected SQL file name {path.name}: want NNNN_name.sql")
        files.append(SqlFile(version=int(match.group(1)), path=path))
    if not files:
        raise FileNotFoundError(f"no SQL files in {directory}")
    return files


def applied_versions(conn: psycopg.Connection[Any]) -> set[int]:
    exists = conn.execute(
        "SELECT 1 FROM pg_tables WHERE schemaname = 'ai' AND tablename = 'schema_version'"
    ).fetchone()
    if not exists:
        return set()
    cur = conn.cursor(row_factory=tuple_row)
    rows = cur.execute("SELECT version FROM ai.schema_version").fetchall()
    return {int(row[0]) for row in rows}


def install(conn: psycopg.Connection[Any], directory: Path | None = None) -> list[int]:
    """Apply every SQL file not yet recorded, one transaction each. Returns applied versions.

    Refuses a database where ai-db was installed as an extension: there the files arrive through
    ALTER EXTENSION aicol UPDATE, and applying them here would detach objects from it.
    """
    if conn.execute("SELECT 1 FROM pg_extension WHERE extname = 'aicol'").fetchone():
        raise InstalledAsExtension(
            "ai-db is installed as the extension aicol:"
            " upgrade it with ALTER EXTENSION aicol UPDATE"
        )
    applied = applied_versions(conn)
    done: list[int] = []
    for sql_file in sql_files(directory):
        if sql_file.version in applied:
            continue
        with conn.transaction():
            conn.execute(sql_file.path.read_text(encoding="utf-8"))
            conn.execute("INSERT INTO ai.schema_version (version) VALUES (%s)", (sql_file.version,))
        done.append(sql_file.version)
    return done
