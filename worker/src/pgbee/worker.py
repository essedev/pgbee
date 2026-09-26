"""The worker loop: claim a batch, run each backend, report back, wait for work.

A Worker serves some backends through one connection. The WorkerPool runs one Worker per
backend, each on its own connection, so a fast backend (decision, embedding) never waits for a
slow one (llm) to finish its batch.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

import psycopg
import structlog

from pgbee.db import WORKER_BACKENDS, Contract
from pgbee.jobs import Job
from pgbee.providers import LlmResult, Provider, ProviderError, classify

log = structlog.get_logger("pgbee.worker")


@dataclass
class BatchStats:
    claimed: int = 0
    outcomes: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    failed: int = 0
    rate_limited: bool = False


class Worker:
    def __init__(
        self,
        contract: Contract,
        provider: Provider,
        *,
        worker_id: str,
        batch_size: int = 20,
        poll_interval: float = 5.0,
        claim_timeout_seconds: int = 300,
        rate_limit_pause: float = 10.0,
        maintenance_interval: float = 3600.0,
        backends: Sequence[str] = WORKER_BACKENDS,
        housekeeping: bool = True,
        stop_event: asyncio.Event | None = None,
    ) -> None:
        unknown = set(backends) - set(WORKER_BACKENDS)
        if unknown or not backends:
            raise ValueError(f"backends must be a non empty subset of {WORKER_BACKENDS}")
        self._db = contract
        self._provider = provider
        self._worker_id = worker_id
        self._batch_size = batch_size
        self._poll_interval = poll_interval
        self._claim_timeout = claim_timeout_seconds
        self._rate_limit_pause = rate_limit_pause
        self._maintenance_interval = maintenance_interval
        self._next_maintenance = 0.0
        self._backends = list(backends)
        self._housekeeping = housekeeping
        self._stop = stop_event or asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def run_forever(self) -> None:
        await self._db.listen()
        log.info(
            "worker.start",
            worker_id=self._worker_id,
            backends=self._backends,
            batch_size=self._batch_size,
        )
        while not self._stop.is_set():
            if self._housekeeping:
                if time.monotonic() >= self._next_maintenance:
                    await self._maintain()
                    self._next_maintenance = time.monotonic() + self._maintenance_interval
                reclaimed = await self._db.reclaim_stale(self._claim_timeout)
                if reclaimed:
                    log.warning("jobs.reclaimed", count=reclaimed)
            stats = await self.run_once()
            if stats.rate_limited:
                await self._pause(self._rate_limit_pause)
                continue
            if stats.claimed >= self._batch_size:
                continue
            await self._idle()
        log.info("worker.stop", worker_id=self._worker_id)

    async def _maintain(self) -> None:
        """Prune old lineage and done jobs in bounded batches. A failure is logged and retried
        at the next interval: maintenance must not stop the queue."""
        results = jobs = 0
        try:
            for _ in range(MAX_PRUNE_BATCHES):
                r, j = await self._db.prune(PRUNE_BATCH)
                results += r
                jobs += j
                if (r < PRUNE_BATCH and j < PRUNE_BATCH) or self._stop.is_set():
                    break
        except psycopg.Error as exc:
            log.error("maintenance.failed", error=str(exc))
            return
        if results or jobs:
            log.info("maintenance.pruned", results=results, jobs=jobs)

    async def _pause(self, seconds: float) -> None:
        """Sleep, but return as soon as stop() is called."""
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)

    async def _idle(self) -> None:
        """Wait for a NOTIFY, the poll interval or stop(), whichever comes first."""
        notified = asyncio.create_task(self._db.wait_for_notify(self._poll_interval))
        stopped = asyncio.create_task(self._stop.wait())
        _, pending = await asyncio.wait({notified, stopped}, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

    async def run_once(self) -> BatchStats:
        """Claim one batch and process it fully. Returns what happened."""
        stats = BatchStats()
        jobs = await self._db.claim(self._worker_id, self._batch_size, self._backends)
        stats.claimed = len(jobs)
        if not jobs:
            return stats
        groups: dict[tuple[int, str], list[Job]] = defaultdict(list)
        for job in jobs:
            if job.backend != "decision":
                groups[(job.column_def_id, job.backend)].append(job)
        decisions = [job for job in jobs if job.backend == "decision"]
        await asyncio.gather(
            *(self._run_group(backend, group, stats) for (_, backend), group in groups.items()),
            self._run_decisions(decisions, stats),
        )
        log.info(
            "batch.done",
            claimed=stats.claimed,
            failed=stats.failed,
            **{f"outcome_{k}": v for k, v in stats.outcomes.items()},
        )
        return stats

    async def _run_group(self, backend: str, jobs: list[Job], stats: BatchStats) -> None:
        if backend == "llm":
            concurrency = int(jobs[0].config.get("concurrency", 4))
            semaphore = asyncio.Semaphore(max(1, concurrency))

            async def one(job: Job) -> None:
                async with semaphore:
                    await self._run_llm(job, stats)

            await asyncio.gather(*(one(job) for job in jobs))
        elif backend == "embedding":
            call_size = int(jobs[0].backend_config.get("batch_size", 100))
            for start in range(0, len(jobs), max(1, call_size)):
                await self._run_embedding(jobs[start : start + call_size], stats)
        else:
            for job in jobs:
                await self._fail(
                    job,
                    ProviderError(f"backend {backend} not handled by this worker", retryable=False),
                    stats,
                )

    async def _run_llm(self, job: Job, stats: BatchStats) -> None:
        """One row, one call: structured generation."""
        try:
            result = await self._provider.derive(job)
        except Exception as exc:
            await self._fail(job, classify(exc), stats)
            return
        await self._complete(job, result, stats)

    async def _run_decisions(self, jobs: list[Job], stats: BatchStats) -> None:
        """Decision jobs on the same row and model, from any column, share one call."""
        if not jobs:
            return
        calls = fan_out(jobs)
        concurrency = int(jobs[0].config.get("concurrency", 4))
        semaphore = asyncio.Semaphore(max(1, concurrency))

        async def one(call: list[Job]) -> None:
            async with semaphore:
                await self._run_decision_call(call, stats)

        await asyncio.gather(*(one(call) for call in calls))

    async def _run_decision_call(self, jobs: list[Job], stats: BatchStats) -> None:
        try:
            results = await self._provider.decide(jobs)
            if len(results) != len(jobs):
                raise ProviderError(
                    f"decision count mismatch: {len(results)} for {len(jobs)} questions",
                    retryable=True,
                )
        except Exception as exc:
            error = classify(exc)
            for job in jobs:
                await self._fail(job, error, stats)
            return
        for job, result in zip(jobs, results, strict=True):
            if isinstance(result, ProviderError):
                await self._fail(job, result, stats)
            else:
                await self._complete(job, result, stats)

    async def _complete(self, job: Job, result: LlmResult, stats: BatchStats) -> None:
        outcome = await self._db.complete(
            job,
            result.value,
            confidence=result.confidence,
            model=result.model,
            usage=result.usage,
            latency_ms=result.latency_ms,
            details=result.details,
        )
        stats.outcomes[outcome] += 1
        log.info(
            "job.done",
            job_id=job.job_id,
            column_def_id=job.column_def_id,
            outcome=outcome,
            confidence=result.confidence,
            latency_ms=result.latency_ms,
            **{f"usage_{k}": v for k, v in result.usage.items()},
        )

    async def _run_embedding(self, jobs: list[Job], stats: BatchStats) -> None:
        texts = [job.source_text() for job in jobs]
        try:
            result = await self._provider.embed(jobs[0].model, texts, jobs[0].backend_config)
            if len(result.vectors) != len(jobs):
                raise ProviderError(
                    f"embedding count mismatch: {len(result.vectors)} for {len(jobs)} inputs",
                    retryable=True,
                )
        except Exception as exc:
            error = classify(exc)
            for job in jobs:
                await self._fail(job, error, stats)
            return
        per_job_latency = result.latency_ms // max(1, len(jobs))
        usage = _split_usage(result.usage, len(jobs))
        for job, vector in zip(jobs, result.vectors, strict=True):
            outcome = await self._db.complete(
                job,
                vector,
                confidence=None,
                model=result.model,
                usage=usage,
                latency_ms=per_job_latency,
            )
            stats.outcomes[outcome] += 1
        log.info(
            "embedding.done", count=len(jobs), model=result.model, latency_ms=result.latency_ms
        )

    async def _fail(self, job: Job, error: ProviderError, stats: BatchStats) -> None:
        stats.failed += 1
        if error.rate_limited:
            stats.rate_limited = True
        status = await self._db.fail(job, str(error), retryable=error.retryable)
        log.warning(
            "job.failed",
            job_id=job.job_id,
            column_def_id=job.column_def_id,
            attempts=job.attempts,
            retryable=error.retryable,
            status=status,
            error=str(error),
        )


class WorkerPool:
    """One Worker per backend, each with its own connection (a connection waiting for NOTIFY
    cannot run queries). They share the stop signal; only the first does maintenance and
    reclaims abandoned jobs."""

    def __init__(
        self,
        connect: Callable[[], Awaitable[Contract]],
        provider: Provider,
        *,
        worker_id: str,
        backends: Sequence[str] = WORKER_BACKENDS,
        **worker_options: Any,
    ) -> None:
        unknown = set(backends) - set(WORKER_BACKENDS)
        if unknown or not backends:
            raise ValueError(f"backends must be a non empty subset of {WORKER_BACKENDS}")
        self._connect = connect
        self._provider = provider
        self._worker_id = worker_id
        self._backends = list(dict.fromkeys(backends))
        self._options = worker_options
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def run_forever(self) -> None:
        contracts: list[Contract] = []
        try:
            workers = []
            for i, backend in enumerate(self._backends):
                contract = await self._connect()
                contracts.append(contract)
                workers.append(
                    Worker(
                        contract,
                        self._provider,
                        worker_id=f"{self._worker_id}/{backend}",
                        backends=[backend],
                        housekeeping=i == 0,
                        stop_event=self._stop,
                        **self._options,
                    )
                )
            await asyncio.gather(*(w.run_forever() for w in workers))
        finally:
            for contract in contracts:
                await contract.close()


MAX_QUESTIONS_PER_CALL = 16
PRUNE_BATCH = 10_000
MAX_PRUNE_BATCHES = 100


def fan_out(jobs: list[Job]) -> list[list[Job]]:
    """Group decision jobs by (model, row, source) and cap the questions per call."""
    groups: dict[tuple[str, str, str], list[Job]] = defaultdict(list)
    for job in jobs:
        key = (
            job.model,
            json.dumps(job.row_pk, sort_keys=True),
            json.dumps(job.source, sort_keys=True, default=str),
        )
        groups[key].append(job)
    return [
        group[i : i + MAX_QUESTIONS_PER_CALL]
        for group in groups.values()
        for i in range(0, len(group), MAX_QUESTIONS_PER_CALL)
    ]


def _split_usage(usage: dict[str, object], n: int) -> dict[str, object]:
    """Spread a batch usage over its jobs so per-column cost sums stay right."""
    if not usage or n <= 0:
        return {}
    out: dict[str, object] = {}
    for key, value in usage.items():
        if isinstance(value, int | float):
            out[key] = value / n
        else:
            out[key] = value
    return out
