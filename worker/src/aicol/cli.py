"""`aicol` command line: install the extension, run the worker, inspect derived columns."""

from __future__ import annotations

import asyncio
import logging
import signal
from typing import Any

import psycopg
import structlog
import typer
from psycopg.rows import dict_row

from aicol.db import Contract
from aicol.installer import install
from aicol.providers import OpenRouterProvider
from aicol.settings import Settings, load_settings
from aicol.worker import Worker

app = typer.Typer(no_args_is_help=True, add_completion=False)


def _configure_logging(level: str) -> None:
    numeric = logging.getLevelName(level.upper())
    if not isinstance(numeric, int):
        numeric = logging.INFO
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(numeric),
        processors=[
            structlog.processors.TimeStamper(fmt="%H:%M:%S"),
            structlog.processors.add_log_level,
            structlog.dev.ConsoleRenderer(),
        ],
    )


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
            "SELECT c.table_schema, c.table_name, c.column_name, c.version, c.backend, c.model,"
            " c.enabled, c.pending, c.claimed, c.done, c.dead, c.stale, c.human_overrides,"
            " c.backfill_pending, c.backfill_scanned,"
            " b.budget_usd, b.budget_period, b.spent_usd, b.exhausted"
            " FROM ai.columns c JOIN ai.budgets b ON b.column_def_id = c.id"
            " ORDER BY c.table_schema, c.table_name, c.column_name"
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
            f"{_backfill(r)}  {_spend(r)}"
        )


def _backfill(r: dict[str, Any]) -> str:
    if not r["backfill_pending"]:
        return ""
    return f" backfilling (scanned {r['backfill_scanned']} rows)"


def _spend(r: dict[str, Any]) -> str:
    spent = f"${r['spent_usd']:.4f}"
    if r["budget_usd"] is None:
        return f"spent {spent} this {r['budget_period']}, no cap"
    flag = " EXHAUSTED" if r["exhausted"] else ""
    return f"spent {spent} of ${r['budget_usd']} per {r['budget_period']}{flag}"


@app.command("run")
def run_cmd(
    once: bool = typer.Option(False, "--once", help="Process one batch and exit."),
    batch_size: int = typer.Option(20, "--batch-size", min=1),
) -> None:
    """Consume the queue: call the models and write the results back."""
    settings = load_settings()
    if not settings.openrouter_api_key:
        raise typer.BadParameter("OPENROUTER_API_KEY is not set")
    _configure_logging(settings.log_level)
    asyncio.run(_run(settings, once=once, batch_size=batch_size))


async def _run(settings: Settings, *, once: bool, batch_size: int) -> None:
    assert settings.openrouter_api_key is not None
    contract = await Contract.connect(settings.database_url)
    provider = OpenRouterProvider(settings.openrouter_api_key, settings.openrouter_base_url)
    worker = Worker(
        contract,
        provider,
        worker_id=settings.worker_id,
        batch_size=batch_size,
        poll_interval=settings.poll_interval_seconds,
        claim_timeout_seconds=settings.claim_timeout_seconds,
    )
    try:
        if once:
            stats = await worker.run_once()
            typer.echo(
                f"claimed={stats.claimed} failed={stats.failed} "
                + " ".join(f"{k}={v}" for k, v in stats.outcomes.items())
            )
            return
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, worker.stop)
        await worker.run_forever()
    finally:
        await contract.close()
