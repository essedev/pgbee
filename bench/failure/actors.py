"""The processes the driver starts and kills: the application of each system and the worker of
the two app-side baselines. pgbee's worker is the real `pgbee run`.

- naive: the application saves the row, commits, then enqueues the row id on an external queue
  (a durable at-least-once queue with leases, like SQS or a Redis list with visibility timeout).
  A worker reads the row, calls the model and writes the value.
- outbox: the application writes the row and the queue entry in one transaction (transactional
  outbox in the same database). Same worker.
- pgbee: the application only writes rows; triggers enqueue.

Every application op commits together with its sequence number, so a restarted application
resumes where it died, like a client retrying a request that did not complete.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
import time
from pathlib import Path

import httpx2 as httpx
import psycopg
from scenario import Op, load, user_message

ENQUEUE_RETRIES = 3


class QueueDown(Exception):
    pass


def queue_up(flag: Path) -> None:
    if flag.exists():
        raise QueueDown("queue unavailable")


def run_app(system: str, db: str, queue_db: str, ops_path: Path, pace: float, flag: Path) -> None:
    ops = load(ops_path)
    # Autocommit, so that each conn.transaction() below is a real transaction, not a savepoint.
    with psycopg.connect(db, autocommit=True) as conn:
        done = conn.execute("SELECT seq FROM bench_progress").fetchone()
        start = done[0] if done else 0
        queue = psycopg.connect(queue_db, autocommit=True) if system == "naive" else None
        for op in ops:
            if op.seq <= start:
                continue
            if system == "pgbee":
                app_pgbee(conn, op)
            elif system == "outbox":
                app_outbox(conn, op)
            else:
                assert queue is not None
                app_naive(conn, queue, op, flag)
            time.sleep(pace)


def _progress(conn: psycopg.Connection[tuple[object, ...]], op: Op) -> None:
    conn.execute("UPDATE bench_progress SET seq = %s", (op.seq,))


def _write(conn: psycopg.Connection[tuple[object, ...]], op: Op) -> None:
    if op.kind == "insert":
        conn.execute("INSERT INTO ticket (id, body) VALUES (%s, %s)", (op.row_id, op.body))
    elif op.kind in ("edit", "script_edit"):
        conn.execute("UPDATE ticket SET body = %s WHERE id = %s", (op.body, op.row_id))
    elif op.kind == "fix":
        conn.execute("UPDATE ticket SET category = %s WHERE id = %s", (op.value, op.row_id))


def app_pgbee(conn: psycopg.Connection[tuple[object, ...]], op: Op) -> None:
    with conn.transaction():
        if op.kind == "prompt":
            conn.execute(
                "SELECT bee.update_column('ticket', 'category', p_prompt => %s)", (op.prompt,)
            )
        else:
            _write(conn, op)
        _progress(conn, op)


def app_outbox(conn: psycopg.Connection[tuple[object, ...]], op: Op) -> None:
    with conn.transaction():
        if op.kind == "prompt":
            conn.execute("UPDATE bench_config SET prompt = %s", (op.prompt,))
            conn.execute("INSERT INTO job (row_id) SELECT id FROM ticket")
        else:
            _write(conn, op)
            if op.kind in ("insert", "edit"):
                conn.execute("INSERT INTO job (row_id) VALUES (%s)", (op.row_id,))
        _progress(conn, op)


def app_naive(
    conn: psycopg.Connection[tuple[object, ...]],
    queue: psycopg.Connection[tuple[object, ...]],
    op: Op,
    flag: Path,
) -> None:
    if op.kind == "prompt":
        # The reprocess script: new prompt, then every row back on the queue. It is rerun until
        # it completes, so its progress is recorded only at the end.
        while True:
            try:
                with conn.transaction():
                    conn.execute("UPDATE bench_config SET prompt = %s", (op.prompt,))
                ids = [r[0] for r in conn.execute("SELECT id FROM ticket ORDER BY id").fetchall()]
                for start in range(0, len(ids), 500):
                    queue_up(flag)
                    with queue.cursor() as cur:
                        cur.executemany(
                            "INSERT INTO job (row_id) VALUES (%s)",
                            [(i,) for i in ids[start : start + 500]],
                        )
                with conn.transaction():
                    _progress(conn, op)
                return
            except QueueDown:
                time.sleep(1)
    with conn.transaction():
        _write(conn, op)
        _progress(conn, op)
    if op.kind not in ("insert", "edit"):
        return
    # The request handler enqueues after the commit, with a few retries. When the queue stays
    # down the error is logged and the request fails, but the row is already saved.
    for attempt in range(ENQUEUE_RETRIES):
        try:
            queue_up(flag)
            queue.execute("INSERT INTO job (row_id) VALUES (%s)", (op.row_id,))
            return
        except QueueDown:
            time.sleep(0.1 * (attempt + 1))
    print(f"enqueue failed for row {op.row_id}", file=sys.stderr)


async def run_worker(
    system: str, db: str, queue_db: str, model_url: str, concurrency: int, lease: int, flag: Path
) -> None:
    async with httpx.AsyncClient(timeout=30) as http:
        await asyncio.gather(
            *(
                _worker_slot(system, db, queue_db, model_url, lease, flag, http)
                for _ in range(concurrency)
            )
        )


async def _worker_slot(
    system: str,
    db: str,
    queue_db: str,
    model_url: str,
    lease: int,
    flag: Path,
    http: httpx.AsyncClient,
) -> None:
    conn = await psycopg.AsyncConnection.connect(db, autocommit=True)
    queue = (
        conn
        if system == "outbox"
        else await psycopg.AsyncConnection.connect(queue_db, autocommit=True)
    )
    naive = system == "naive"
    while True:
        try:
            if naive:
                queue_up(flag)
            cur = await queue.execute(
                "UPDATE job SET locked_until = now() + make_interval(secs => %s)"
                " WHERE id = (SELECT id FROM job WHERE locked_until < now() ORDER BY id"
                " LIMIT 1 FOR UPDATE SKIP LOCKED) RETURNING id, row_id",
                (lease,),
            )
            job = await cur.fetchone()
            if job is None:
                await asyncio.sleep(0.2)
                continue
            job_id, row_id = job
            row = await (
                await conn.execute("SELECT body FROM ticket WHERE id = %s", (row_id,))
            ).fetchone()
            config = await (await conn.execute("SELECT prompt FROM bench_config")).fetchone()
            assert row is not None and config is not None
            response = await http.post(
                model_url,
                json={
                    "model": f"fake/{system}",
                    "messages": [{"role": "user", "content": user_message(config[0], row[0])}],
                },
            )
            if response.status_code != 200:
                # Retry soon: release the lease instead of waiting for it to expire.
                await queue.execute(
                    "UPDATE job SET locked_until = now() + interval '1 second' WHERE id = %s",
                    (job_id,),
                )
                continue
            content = response.json()["choices"][0]["message"]["content"]
            value = json.loads(content)["value"]
            await conn.execute("UPDATE ticket SET category = %s WHERE id = %s", (value, row_id))
            if naive:
                queue_up(flag)
            await queue.execute("DELETE FROM job WHERE id = %s", (job_id,))
        except (QueueDown, httpx.TransportError):
            await asyncio.sleep(random.uniform(0.2, 0.6))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("role", choices=["app", "worker"])
    parser.add_argument("--system", choices=["pgbee", "naive", "outbox"], required=True)
    parser.add_argument("--db", required=True)
    parser.add_argument("--queue-db", default="")
    parser.add_argument("--ops", type=Path)
    parser.add_argument("--pace", type=float, default=0.02)
    parser.add_argument("--model-url", default="")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--lease", type=int, default=10)
    parser.add_argument("--outage-flag", type=Path, required=True)
    args = parser.parse_args()
    if args.role == "app":
        run_app(args.system, args.db, args.queue_db, args.ops, args.pace, args.outage_flag)
    else:
        asyncio.run(
            run_worker(
                args.system,
                args.db,
                args.queue_db,
                args.model_url,
                args.concurrency,
                args.lease,
                args.outage_flag,
            )
        )


if __name__ == "__main__":
    main()
