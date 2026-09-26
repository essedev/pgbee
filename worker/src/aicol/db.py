"""The worker's side of the SQL contract: claim, complete, fail, reclaim, listen."""

from __future__ import annotations

import json
from typing import Any

import psycopg
from psycopg.rows import DictRow, dict_row

from aicol.jobs import Job

WORKER_BACKENDS = ["llm", "decision", "embedding"]


class Contract:
    def __init__(self, conn: psycopg.AsyncConnection[DictRow]) -> None:
        self._conn = conn

    @classmethod
    async def connect(cls, database_url: str) -> Contract:
        conn = await psycopg.AsyncConnection.connect(
            database_url, autocommit=True, row_factory=dict_row
        )
        return cls(conn)

    async def close(self) -> None:
        await self._conn.close()

    async def claim(self, worker_id: str, batch_size: int) -> list[Job]:
        cur = await self._conn.execute(
            "SELECT * FROM ai.claim_jobs(%s, %s, %s::ai.backend[]) ORDER BY column_def_id, job_id",
            (worker_id, batch_size, WORKER_BACKENDS),
        )
        return [Job.from_row(row) for row in await cur.fetchall()]

    async def complete(
        self,
        job: Job,
        value: Any,
        *,
        confidence: float | None,
        model: str,
        usage: dict[str, Any],
        latency_ms: int,
        details: dict[str, Any] | None = None,
    ) -> str:
        cur = await self._conn.execute(
            "SELECT ai.complete_job(%s::bigint, %s, %s::jsonb, %s::real, %s, %s::jsonb, %s,"
            " %s::jsonb) AS outcome",
            (
                job.job_id,
                job.source_hash,
                json.dumps(value),
                confidence,
                model,
                json.dumps(usage),
                latency_ms,
                None if details is None else json.dumps(details),
            ),
        )
        row = await cur.fetchone()
        assert row is not None
        return str(row["outcome"])

    async def fail(self, job: Job, error: str, *, retryable: bool) -> str | None:
        cur = await self._conn.execute(
            "SELECT ai.fail_job(%s::bigint, %s, %s) AS status",
            (job.job_id, error[:2000], retryable),
        )
        row = await cur.fetchone()
        assert row is not None
        return None if row["status"] is None else str(row["status"])

    async def reclaim_stale(self, timeout_seconds: int) -> int:
        cur = await self._conn.execute(
            "SELECT ai.reclaim_stale(make_interval(secs => %s)) AS n", (timeout_seconds,)
        )
        row = await cur.fetchone()
        assert row is not None
        return int(row["n"])

    async def listen(self) -> None:
        await self._conn.execute("LISTEN ai_jobs")

    async def wait_for_notify(self, max_wait: float) -> bool:
        """Block until a NOTIFY on ai_jobs arrives or max_wait seconds pass. True on notify."""
        gen = self._conn.notifies(timeout=max_wait, stop_after=1)
        async for _ in gen:
            return True
        return False
