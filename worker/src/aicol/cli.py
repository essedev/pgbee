"""`aicol` command line: install the extension, inspect derived columns."""

from __future__ import annotations

import psycopg
import typer
from psycopg.rows import dict_row

from aicol.installer import install
from aicol.settings import load_settings

app = typer.Typer(no_args_is_help=True, add_completion=False)


@app.command("install")
def install_cmd() -> None:
    """Apply the SQL files in sql/ that the database has not seen yet."""
    settings = load_settings()
    with psycopg.connect(settings.database_url) as conn:
        applied = install(conn)
    if applied:
        typer.echo(f"applied: {', '.join(str(v) for v in applied)}")
    else:
        typer.echo("up to date")


@app.command("status")
def status_cmd() -> None:
    """List derived columns with their version and queue counters."""
    settings = load_settings()
    with psycopg.connect(settings.database_url, row_factory=dict_row) as conn:
        rows = conn.execute(
            "SELECT table_schema, table_name, column_name, version, backend, model, enabled,"
            " pending, claimed, done, dead, stale, human_overrides"
            " FROM ai.columns ORDER BY table_schema, table_name, column_name"
        ).fetchall()
    if not rows:
        typer.echo("no derived columns")
        return
    for r in rows:
        state = "on" if r["enabled"] else "off"
        typer.echo(
            f"{r['table_schema']}.{r['table_name']}.{r['column_name']}"
            f"  v{r['version']} {r['backend']} {r['model']} [{state}]"
            f"  pending={r['pending']} claimed={r['claimed']} done={r['done']} dead={r['dead']}"
            f" stale={r['stale']} human={r['human_overrides']}"
        )
