"""Apply the versioned SQL files in `sql/` to a database and track them in ai.schema_version."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

import psycopg

SQL_FILE_RE = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


@dataclass(frozen=True)
class SqlFile:
    version: int
    path: Path


def sql_dir() -> Path:
    """Directory holding the extension SQL files.

    Overridable with AICOL_SQL_DIR; defaults to the repository's `sql/` next to `worker/`.
    """
    env = os.environ.get("AICOL_SQL_DIR")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[3] / "sql"


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


def applied_versions(conn: psycopg.Connection) -> set[int]:
    exists = conn.execute(
        "SELECT 1 FROM pg_tables WHERE schemaname = 'ai' AND tablename = 'schema_version'"
    ).fetchone()
    if not exists:
        return set()
    rows = conn.execute("SELECT version FROM ai.schema_version").fetchall()
    return {int(row[0]) for row in rows}


def install(conn: psycopg.Connection, directory: Path | None = None) -> list[int]:
    """Apply every SQL file not yet recorded, one transaction each. Returns applied versions."""
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
