"""`pgbee` command line: install the extension, run the worker, inspect derived columns."""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from pathlib import Path
from typing import Annotated, Any

import psycopg
import structlog
import typer
from psycopg.rows import dict_row

from pgbee import extension
from pgbee.db import WORKER_BACKENDS, Contract
from pgbee.installer import InstalledAsExtension, install
from pgbee.providers import OpenAICompatibleProvider, OpenRouterProvider, Provider
from pgbee.settings import Settings, load_settings
from pgbee.worker import Worker, WorkerPool

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
            structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty()),
        ],
    )


@app.command("install")
def install_cmd() -> None:
    """Apply the SQL files in sql/ that the database has not seen yet."""
    settings = load_settings()
    with psycopg.connect(settings.database_url) as conn:
        try:
            applied = install(conn)
        except InstalledAsExtension as exc:
            raise typer.BadParameter(str(exc)) from exc
    if applied:
        typer.echo(f"applied: {', '.join(str(v) for v in applied)}")
    else:
        typer.echo("up to date")


@app.command("extension-files")
def extension_files_cmd(
    out_dir: Annotated[Path, typer.Argument(help="Where to write pgbee.control and the scripts.")],
) -> None:
    """Write the files for CREATE EXTENSION pgbee, to copy into `pg_config --sharedir`/extension."""
    for path in extension.build(out_dir):
        typer.echo(path)


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
            " FROM bee.columns c JOIN bee.budgets b ON b.column_def_id = c.id"
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


OPENAI_DEFAULT_URL = "https://api.openai.com/v1"


def build_provider(settings: Settings) -> Provider:
    try:
        kind = settings.provider_kind()
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    if kind == "openrouter":
        assert settings.openrouter_api_key is not None
        return OpenRouterProvider(
            settings.openrouter_api_key,
            settings.openrouter_base_url,
            timeout=settings.request_timeout_seconds,
        )
    # Local servers (Ollama, vLLM) take no key, but the SDK wants a non empty one.
    return OpenAICompatibleProvider(
        settings.openai_api_key or "not-needed",
        settings.openai_base_url or OPENAI_DEFAULT_URL,
        timeout=settings.request_timeout_seconds,
    )


@app.command("run")
def run_cmd(
    once: bool = typer.Option(False, "--once", help="Process one batch and exit."),
    batch_size: int = typer.Option(20, "--batch-size", min=1),
    backends: str = typer.Option(
        "",
        "--backends",
        help="Comma separated backends this worker serves, one lane each. Default: all the"
        f" provider supports ({','.join(WORKER_BACKENDS)} on OpenRouter).",
    ),
) -> None:
    """Consume the queue: call the models and write the results back."""
    settings = load_settings()
    provider = build_provider(settings)
    _configure_logging(settings.log_level)
    chosen = [b.strip() for b in backends.split(",") if b.strip()] or list(provider.backends)
    if set(chosen) - set(WORKER_BACKENDS):
        raise typer.BadParameter(f"--backends takes a subset of {','.join(WORKER_BACKENDS)}")
    unsupported = set(chosen) - set(provider.backends)
    if unsupported:
        raise typer.BadParameter(
            f"{','.join(sorted(unsupported))} not available with {settings.provider_kind()}:"
            " the decision backend needs OpenRouter"
        )
    asyncio.run(_run(settings, provider, once=once, batch_size=batch_size, backends=chosen))


async def _run(
    settings: Settings, provider: Provider, *, once: bool, batch_size: int, backends: list[str]
) -> None:
    structlog.get_logger("pgbee").info("provider", kind=settings.provider_kind(), backends=backends)
    options: dict[str, Any] = {
        "batch_size": batch_size,
        "poll_interval": settings.poll_interval_seconds,
        "claim_timeout_seconds": settings.claim_timeout_seconds,
        "maintenance_interval": settings.maintenance_interval_seconds,
    }
    if once:
        contract = await Contract.connect(settings.database_url)
        try:
            worker = Worker(
                contract, provider, worker_id=settings.worker_id, backends=backends, **options
            )
            stats = await worker.run_once()
        finally:
            await contract.close()
        typer.echo(
            f"claimed={stats.claimed} failed={stats.failed} "
            + " ".join(f"{k}={v}" for k, v in stats.outcomes.items())
        )
        return
    pool = WorkerPool(
        lambda: Contract.connect(settings.database_url),
        provider,
        worker_id=settings.worker_id,
        backends=backends,
        **options,
    )
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, pool.stop)
    await pool.run_forever()
