"""The worker as a long running process: wakes on NOTIFY, drains the backlog, reclaims, stops."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any

import psycopg
import pytest
from fakes import FakeProvider, add_urgency
from psycopg.rows import DictRow

from pgbee.db import Contract
from pgbee.jobs import Job
from pgbee.providers import LlmResult, Provider, ProviderError
from pgbee.worker import Worker, WorkerPool


async def wait_until(check: Callable[[], bool], limit: float = 5.0) -> float:
    """Poll a sync predicate until true; return the seconds it took. Fails past the limit."""
    started = time.monotonic()
    while not check():
        if time.monotonic() - started > limit:
            raise AssertionError(f"condition not met within {limit}s")
        await asyncio.sleep(0.05)
    return time.monotonic() - started


def filled(conn: psycopg.Connection[DictRow], ticket_id: int) -> bool:
    row = conn.execute("SELECT urgency FROM ticket WHERE id = %s", (ticket_id,)).fetchone()
    return row is not None and row["urgency"] is not None


async def start(
    database_url: str, provider: Provider, **kwargs: Any
) -> tuple[Worker, Contract, asyncio.Task[None]]:
    contract = await Contract.connect(database_url)
    worker = Worker(contract, provider, worker_id="daemon", **kwargs)
    task = asyncio.create_task(worker.run_forever())
    return worker, contract, task


async def shutdown(worker: Worker, contract: Contract, task: asyncio.Task[None]) -> None:
    worker.stop()
    await asyncio.wait_for(task, timeout=5)
    await contract.close()


async def test_daemon_wakes_on_insert_without_waiting_for_the_poll(
    database_url: str, conn: psycopg.Connection[DictRow], ticket: str
) -> None:
    add_urgency(conn)
    worker, contract, task = await start(database_url, FakeProvider(), poll_interval=30)
    try:
        await wait_until(lambda: filled(conn, 3))
        row = conn.execute(
            "INSERT INTO ticket (body) VALUES ('Urgente: ordini bloccati') RETURNING id"
        ).fetchone()
        assert row is not None
        took = await wait_until(lambda: filled(conn, row["id"]))
        assert took < 3, "the insert must wake the worker, not the 30s poll"
    finally:
        await shutdown(worker, contract, task)


async def test_wait_for_notify_drains_the_backlog_in_one_go(
    database_url: str, conn: psycopg.Connection[DictRow], ticket: str
) -> None:
    add_urgency(conn)
    contract = await Contract.connect(database_url)
    try:
        await contract.listen()
        for i in range(5):  # five transactions, five notifications
            conn.execute("INSERT INTO ticket (body) VALUES (%s)", (f"ticket {i}",))
        await contract.reclaim_stale(300)  # any round trip reads the queued notifications
        assert await contract.wait_for_notify(0.5) is True
        assert await contract.wait_for_notify(0.2) is False
    finally:
        await contract.close()


class SlowProvider(FakeProvider):
    def __init__(self, delay: float) -> None:
        super().__init__()
        self.delay = delay

    async def derive(self, job: Job) -> LlmResult:
        await asyncio.sleep(self.delay)
        return await super().derive(job)


async def test_stop_lets_the_current_batch_finish(
    database_url: str, conn: psycopg.Connection[DictRow], ticket: str
) -> None:
    add_urgency(conn)
    provider = SlowProvider(0.3)
    worker, contract, task = await start(database_url, provider, poll_interval=30)
    await wait_until(lambda: len(provider.calls) > 0)
    await shutdown(worker, contract, task)
    counts = conn.execute(
        "SELECT count(*) FILTER (WHERE status = 'claimed') AS claimed,"
        " count(*) FILTER (WHERE status = 'done') AS done FROM bee.job"
    ).fetchone()
    assert counts == {"claimed": 0, "done": 3}


async def test_daemon_reclaims_jobs_abandoned_by_a_dead_worker(
    database_url: str, conn: psycopg.Connection[DictRow], ticket: str
) -> None:
    add_urgency(conn)
    claimed = conn.execute("SELECT count(*) AS n FROM bee.claim_jobs('crashed', 10)").fetchone()
    assert claimed == {"n": 3}
    conn.execute("UPDATE bee.job SET claimed_at = now() - interval '10 minutes'")
    worker, contract, task = await start(
        database_url, FakeProvider(), poll_interval=0.2, claim_timeout_seconds=60
    )
    try:
        await wait_until(lambda: all(filled(conn, i) for i in (1, 2, 3)))
    finally:
        await shutdown(worker, contract, task)


async def test_stop_interrupts_the_rate_limit_pause(
    database_url: str, conn: psycopg.Connection[DictRow], ticket: str
) -> None:
    add_urgency(conn)
    provider = FakeProvider(fail_with=ProviderError("429", retryable=True, rate_limited=True))
    worker, contract, task = await start(database_url, provider, rate_limit_pause=30)
    await wait_until(lambda: len(provider.calls) == 3)
    started = time.monotonic()
    await shutdown(worker, contract, task)
    assert time.monotonic() - started < 2
    pending = conn.execute("SELECT count(*) AS n FROM bee.job WHERE status = 'pending'").fetchone()
    assert pending == {"n": 3}, "rate limited jobs go back to the queue with backoff"


async def test_daemon_drives_a_chunked_backfill_without_waiting_for_the_poll(
    database_url: str, conn: psycopg.Connection[DictRow], ticket: str
) -> None:
    conn.execute("INSERT INTO ticket (body) SELECT 'ticket ' || g FROM generate_series(4, 12) g")
    add_urgency(conn, backfill_chunk=2)
    worker, contract, task = await start(database_url, FakeProvider(), poll_interval=30)
    try:
        took = await wait_until(lambda: all(filled(conn, i) for i in range(1, 13)))
        assert took < 5, "each chunk wakes the worker for the next one"
    finally:
        await shutdown(worker, contract, task)


async def test_daemon_prunes_on_start_and_every_interval(
    database_url: str, conn: psycopg.Connection[DictRow], ticket: str
) -> None:
    add_urgency(conn, lineage_retention_days=1)
    worker, contract, task = await start(
        database_url, FakeProvider(), poll_interval=0.1, maintenance_interval=0.2
    )
    try:
        # All three: jobs run in parallel, and one still running would get a fresh updated_at.
        await wait_until(lambda: all(filled(conn, i) for i in (1, 2, 3)))
        conn.execute("UPDATE bee.job SET updated_at = now() - interval '8 days'")
        await wait_until(
            lambda: conn.execute("SELECT count(*) AS n FROM bee.job").fetchone() == {"n": 0}
        )
    finally:
        await shutdown(worker, contract, task)


async def test_failed_maintenance_does_not_stop_the_queue(
    database_url: str, conn: psycopg.Connection[DictRow], ticket: str
) -> None:
    add_urgency(conn)
    worker, contract, task = await start(database_url, FakeProvider(), poll_interval=30)

    async def broken(limit: int) -> tuple[int, int]:
        raise psycopg.errors.UndefinedFunction("function bee.prune(integer) does not exist")

    contract.prune = broken  # type: ignore[method-assign]
    try:
        await wait_until(lambda: filled(conn, 3))
        assert not task.done()
    finally:
        await shutdown(worker, contract, task)


def add_blocking_decision(conn: psycopg.Connection[DictRow]) -> None:
    conn.execute(
        "SELECT bee.add_column('ticket', 'blocking', array['body'], 'boolean', 'decision',"
        " p_prompt => 'Is the shop blocked?', p_model => 'typesafe/jev-1.13')"
    )


def column_filled(conn: psycopg.Connection[DictRow], column: str) -> int:
    row = conn.execute(f"SELECT count({column}) AS n FROM ticket").fetchone()
    assert row is not None
    return int(row["n"])


async def test_pool_fast_backend_does_not_wait_for_the_slow_one(
    database_url: str, conn: psycopg.Connection[DictRow], ticket: str
) -> None:
    add_urgency(conn)  # llm, 1.5 s per call below
    add_blocking_decision(conn)  # decision, immediate
    pool = WorkerPool(
        lambda: Contract.connect(database_url),
        SlowProvider(1.5),
        worker_id="pool",
        poll_interval=30,
    )
    task = asyncio.create_task(pool.run_forever())
    try:
        await wait_until(lambda: column_filled(conn, "blocking") == 3, limit=1.0)
        assert column_filled(conn, "urgency") == 0, "the llm lane is still working"
        await wait_until(lambda: column_filled(conn, "urgency") == 3)
    finally:
        pool.stop()
        await asyncio.wait_for(task, timeout=5)


async def test_pool_serves_only_its_backends(
    database_url: str, conn: psycopg.Connection[DictRow], ticket: str
) -> None:
    add_urgency(conn)
    add_blocking_decision(conn)
    pool = WorkerPool(
        lambda: Contract.connect(database_url),
        FakeProvider(),
        worker_id="pool",
        backends=["decision"],
        poll_interval=30,
    )
    task = asyncio.create_task(pool.run_forever())
    try:
        await wait_until(lambda: column_filled(conn, "blocking") == 3)
        claimers = conn.execute("SELECT DISTINCT claimed_by FROM bee.job WHERE status = 'done'")
        assert claimers.fetchall() == [{"claimed_by": "pool/decision"}]
        assert column_filled(conn, "urgency") == 0
    finally:
        pool.stop()
        await asyncio.wait_for(task, timeout=5)


def test_backends_are_validated() -> None:
    with pytest.raises(ValueError, match="backends"):
        WorkerPool(Contract.connect, FakeProvider(), worker_id="x", backends=["custom"])  # type: ignore[arg-type]


async def test_drain_empties_every_lane_then_returns(
    database_url: str, conn: psycopg.Connection[DictRow], ticket: str
) -> None:
    add_urgency(conn)
    add_blocking_decision(conn)
    # A run killed by its scheduler left one job claimed: the drain gives it back first.
    conn.execute("SELECT * FROM bee.claim_jobs('killed-run', 1, array['llm']::bee.backend[])")
    conn.execute("UPDATE bee.job SET claimed_at = now() - interval '10 minutes'")
    pool = WorkerPool(
        lambda: Contract.connect(database_url),
        FakeProvider(),
        worker_id="cron",
        batch_size=1,
        claim_timeout_seconds=60,
    )
    totals = await asyncio.wait_for(pool.drain(), timeout=10)
    assert totals["llm"].claimed == 3 and totals["decision"].claimed == 3
    assert totals["embedding"].claimed == 0
    assert totals["llm"].outcomes == {"written": 3}
    assert column_filled(conn, "urgency") == 3 and column_filled(conn, "blocking") == 3


async def test_drain_stops_claiming_at_the_deadline(
    database_url: str, conn: psycopg.Connection[DictRow], ticket: str
) -> None:
    add_urgency(conn)
    pool = WorkerPool(
        lambda: Contract.connect(database_url),
        SlowProvider(1.0),
        worker_id="cron",
        backends=["llm"],
        batch_size=1,
    )
    started = time.monotonic()
    totals = await pool.drain(started + 1.5)
    # One row per second from whenever the lane is connected: one or two batches start before
    # the deadline, the third never does, and the batch in flight is finished.
    assert totals["llm"].claimed in (1, 2)
    assert column_filled(conn, "urgency") == totals["llm"].claimed
    assert time.monotonic() - started < 3.5
