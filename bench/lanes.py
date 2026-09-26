"""One loop for every backend against one lane per backend (decision #20), with simulated model
latencies: nothing is paid and the run is repeatable.

A table of N rows gets three derived columns, one per backend. The provider only waits, for the
median latencies measured in the CFPB field test (llm 1.87 s, decision 0.31 s, embedding 0.03 s
per call), and answers. The same rows are processed twice: by a single Worker that claims every
backend, then by one Worker per backend as `pgbee run` does. For each column the script records
when its last row was filled.

Run from the worker directory, with the demo Postgres up (make db-up):
    uv run python ../bench/lanes.py --rows 200
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import psycopg
import structlog

from pgbee.db import Contract
from pgbee.installer import install
from pgbee.jobs import Job
from pgbee.providers import EmbeddingResult, LlmResult, ProviderError
from pgbee.worker import Worker

COLUMNS = {"summary": "llm", "urgent": "decision", "embedding": "embedding"}


class LatencyProvider:
    """Waits like a model would, answers something valid."""

    backends: tuple[str, ...] = ("llm", "decision", "embedding")

    def __init__(self, llm: float, decision: float, embedding: float) -> None:
        self._llm, self._decision, self._embedding = llm, decision, embedding

    async def derive(self, job: Job) -> LlmResult:
        await asyncio.sleep(self._llm)
        return LlmResult(value="a short summary", confidence=0.9, model="simulated/llm")

    async def decide(self, jobs: list[Job]) -> list[LlmResult | ProviderError]:
        await asyncio.sleep(self._decision)
        return [LlmResult(value=True, confidence=0.9, model="simulated/decision") for _ in jobs]

    async def embed(self, model: str, texts: list[str], config: dict[str, Any]) -> EmbeddingResult:
        await asyncio.sleep(self._embedding)
        return EmbeddingResult(vectors=[[0.1, 0.2, 0.3] for _ in texts], model="simulated/embed")


def prepare(admin_url: str, rows: int) -> str:
    base, _, _ = admin_url.rpartition("/")
    url = f"{base}/bench_lanes"
    with psycopg.connect(admin_url, autocommit=True) as admin:
        admin.execute("DROP DATABASE IF EXISTS bench_lanes WITH (FORCE)")
        admin.execute("CREATE DATABASE bench_lanes")
    with psycopg.connect(url) as conn:
        conn.execute("CREATE EXTENSION vector")
        install(conn)
        conn.commit()
        conn.execute("CREATE TABLE ticket (id serial PRIMARY KEY, body text NOT NULL)")
        conn.execute(
            "SELECT bee.add_column('ticket', 'summary', array['body'], 'text',"
            " p_prompt => 'Summarize', p_model => 'simulated/llm')"
        )
        conn.execute(
            "SELECT bee.add_column('ticket', 'urgent', array['body'], 'boolean', 'decision',"
            " p_prompt => 'Is it urgent?', p_model => 'typesafe/jev-1.13')"
        )
        conn.execute(
            "SELECT bee.add_column('ticket', 'embedding', array['body'], 'vector',"
            " p_backend => 'embedding', p_model => 'simulated/embed',"
            " p_output_schema => '{\"dimensions\": 3}')"
        )
        conn.execute(
            "INSERT INTO ticket (body) SELECT 'ticket ' || g FROM generate_series(1, %s) g",
            (rows,),
        )
    analyze(url)
    return url


def analyze(url: str) -> None:
    """Fresh statistics before each mode: with stale ones the first claims are slow."""
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("ANALYZE ticket, bee.job, bee.result")


def reset(url: str) -> None:
    """Back to empty columns and a full queue, for the second mode."""
    with psycopg.connect(url) as conn:
        conn.execute("DELETE FROM bee.job")
        conn.execute("DELETE FROM bee.result")
        conn.execute("DELETE FROM bee.spend")
        for column in COLUMNS:
            conn.execute(f"SELECT bee.disable('ticket', '{column}')")
        conn.execute("UPDATE ticket SET summary = NULL, urgent = NULL, embedding = NULL")
        for column in COLUMNS:
            conn.execute(f"SELECT bee.enable('ticket', '{column}')")
    analyze(url)


async def watch(url: str, rows: int, started: float, done: dict[str, float]) -> None:
    async with await psycopg.AsyncConnection.connect(url, autocommit=True) as conn:
        while len(done) < len(COLUMNS):
            cur = await conn.execute(
                "SELECT count(summary), count(urgent), count(embedding) FROM ticket"
            )
            counts = await cur.fetchone()
            assert counts is not None
            for column, n in zip(COLUMNS, counts, strict=True):
                if n == rows and column not in done:
                    done[column] = round(time.monotonic() - started, 1)
            await asyncio.sleep(0.1)


async def run_mode(url: str, rows: int, provider: LatencyProvider, lanes: bool) -> dict[str, float]:
    groups = [[b] for b in COLUMNS.values()] if lanes else [list(COLUMNS.values())]
    contracts = [await Contract.connect(url) for _ in groups]
    workers = [
        Worker(c, provider, worker_id=f"bench/{'+'.join(g)}", backends=g, housekeeping=i == 0)
        for i, (c, g) in enumerate(zip(contracts, groups, strict=True))
    ]
    done: dict[str, float] = {}
    started = time.monotonic()
    try:
        await asyncio.gather(watch(url, rows, started, done), *(w.drain() for w in workers))
    finally:
        for c in contracts:
            await c.close()
    return {column: done[column] for column in COLUMNS}


async def main_async(args: argparse.Namespace) -> dict[str, Any]:
    url = prepare(args.admin_url, args.rows)
    provider = LatencyProvider(args.llm, args.decision, args.embedding)
    single = await run_mode(url, args.rows, provider, lanes=False)
    reset(url)
    pool = await run_mode(url, args.rows, provider, lanes=True)
    return {
        "rows": args.rows,
        "latency_s": {"llm": args.llm, "decision": args.decision, "embedding": args.embedding},
        "seconds_until_column_filled": {"one_loop": single, "one_lane_per_backend": pool},
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--rows", type=int, default=200)
    parser.add_argument("--llm", type=float, default=1.87, help="seconds per llm call")
    parser.add_argument("--decision", type=float, default=0.31, help="seconds per decision call")
    parser.add_argument("--embedding", type=float, default=0.03, help="seconds per embedding call")
    parser.add_argument("--out", type=Path, help="write the result as JSON")
    parser.add_argument(
        "--admin-url",
        default=os.environ.get("DATABASE_URL", "postgresql://aidb:aidb@localhost:4460/aidb"),
    )
    args = parser.parse_args()
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING))
    result = asyncio.run(main_async(args))
    print(json.dumps(result, indent=2))
    if args.out:
        args.out.write_text(json.dumps(result, indent=2) + "\n")


if __name__ == "__main__":
    main()
