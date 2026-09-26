"""The worker loop: claim a batch, run each backend, report back, wait for work."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass, field

import structlog

from aicol.db import Contract
from aicol.jobs import Job
from aicol.providers import Provider, ProviderError, classify

log = structlog.get_logger("aicol.worker")


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
    ) -> None:
        self._db = contract
        self._provider = provider
        self._worker_id = worker_id
        self._batch_size = batch_size
        self._poll_interval = poll_interval
        self._claim_timeout = claim_timeout_seconds
        self._rate_limit_pause = rate_limit_pause
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def run_forever(self) -> None:
        await self._db.listen()
        log.info("worker.start", worker_id=self._worker_id, batch_size=self._batch_size)
        while not self._stop.is_set():
            reclaimed = await self._db.reclaim_stale(self._claim_timeout)
            if reclaimed:
                log.warning("jobs.reclaimed", count=reclaimed)
            stats = await self.run_once()
            if stats.rate_limited:
                await asyncio.sleep(self._rate_limit_pause)
                continue
            if stats.claimed >= self._batch_size:
                continue
            await self._db.wait_for_notify(self._poll_interval)
        log.info("worker.stop", worker_id=self._worker_id)

    async def run_once(self) -> BatchStats:
        """Claim one batch and process it fully. Returns what happened."""
        stats = BatchStats()
        jobs = await self._db.claim(self._worker_id, self._batch_size)
        stats.claimed = len(jobs)
        if not jobs:
            return stats
        groups: dict[tuple[int, str], list[Job]] = defaultdict(list)
        for job in jobs:
            groups[(job.column_def_id, job.backend)].append(job)
        await asyncio.gather(
            *(self._run_group(backend, group, stats) for (_, backend), group in groups.items())
        )
        log.info(
            "batch.done",
            claimed=stats.claimed,
            failed=stats.failed,
            **{f"outcome_{k}": v for k, v in stats.outcomes.items()},
        )
        return stats

    async def _run_group(self, backend: str, jobs: list[Job], stats: BatchStats) -> None:
        if backend in ("llm", "decision"):
            concurrency = int(jobs[0].config.get("concurrency", 4))
            semaphore = asyncio.Semaphore(max(1, concurrency))

            async def one(job: Job) -> None:
                async with semaphore:
                    await self._run_single(job, stats)

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

    async def _run_single(self, job: Job, stats: BatchStats) -> None:
        """One row, one call: llm (structured generation) or decision (typed question)."""
        try:
            if job.backend == "decision":
                result = await self._provider.decide(job)
            else:
                result = await self._provider.derive(job)
        except Exception as exc:
            await self._fail(job, classify(exc), stats)
            return
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
