"""Scale on large tables, no model involved (nothing is paid).

- backfill: add_column on a table of N rows. How long the declaration takes, how long a
  concurrent insert waits, how many jobs the first chunk enqueues, how long the first claim takes
  (it also drives the next chunk).
- bulk-trigger: N rows inserted in one statement after the declaration, so the trigger enqueues
  one job per row; then the time of a few claims on the full queue.
- bulk-disabled: the recipe for large loads. Disable the column, load, enable: the rows are
  enqueued in chunks by the backfill.

Run from the worker directory, with the demo Postgres up (make db-up):
    uv run python ../bench/scale.py backfill --rows 1000000
"""

from __future__ import annotations

import argparse
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

import psycopg

from pgbee.installer import install

DECLARE = (
    "SELECT bee.add_column('doc', 'label', array['body'], 'text',"
    " p_prompt => 'Summarize', p_model => 'none/model')"
)
FILL = (
    "INSERT INTO doc (body) SELECT 'document ' || g || repeat(' sample text', 5)"
    " FROM generate_series(1, %s) g"
)


def fresh_database(admin_url: str) -> str:
    base, _, _ = admin_url.rpartition("/")
    url = f"{base}/bench_scale"
    with psycopg.connect(admin_url, autocommit=True) as admin:
        admin.execute("DROP DATABASE IF EXISTS bench_scale WITH (FORCE)")
        admin.execute("CREATE DATABASE bench_scale")
    with psycopg.connect(url) as conn:
        install(conn)
        conn.execute("CREATE TABLE doc (id bigserial PRIMARY KEY, body text NOT NULL)")
    return url


def scalar(conn: psycopg.Connection[Any], sql: str) -> Any:
    row = conn.execute(sql).fetchone()
    assert row is not None
    return row[0]


def claim_ms(conn: psycopg.Connection[Any]) -> tuple[float, int]:
    started = time.monotonic()
    rows = conn.execute("SELECT * FROM bee.claim_jobs('bench', 20)").fetchall()
    return round((time.monotonic() - started) * 1000, 1), len(rows)


def backfill(url: str, rows: int) -> dict[str, Any]:
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(FILL, (rows,))
        conn.execute("VACUUM ANALYZE doc")
    waited: list[float] = []

    def writer() -> None:
        time.sleep(0.2)
        with psycopg.connect(url, autocommit=True) as w:
            started = time.monotonic()
            w.execute("INSERT INTO doc (body) VALUES ('written during the declaration')")
            waited.append(time.monotonic() - started)

    thread = threading.Thread(target=writer)
    with psycopg.connect(url, autocommit=True) as conn:
        thread.start()
        started = time.monotonic()
        conn.execute(DECLARE)
        declare_s = time.monotonic() - started
        thread.join()
        first_chunk = scalar(conn, "SELECT count(*) FROM bee.job")
        first_claim_ms, claimed = claim_ms(conn)
    return {
        "rows": rows,
        "add_column_s": round(declare_s, 3),
        "concurrent_insert_waited_s": round(waited[0], 3),
        "jobs_after_declaration": first_chunk,
        "first_claim_ms": first_claim_ms,
        "claimed": claimed,
    }


def bulk_trigger(url: str, rows: int) -> dict[str, Any]:
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(DECLARE)
        started = time.monotonic()
        conn.execute(FILL, (rows,))
        load_s = time.monotonic() - started
        jobs = scalar(conn, "SELECT count(*) FROM bee.job")
        # Before autovacuum analyzes the new rows the planner works with stale statistics: the
        # first claims are slow. Measured as they come, then after ANALYZE.
        cold = [claim_ms(conn)[0] for _ in range(3)]
        conn.execute("ANALYZE bee.job")
        warm = [claim_ms(conn)[0] for _ in range(3)]
    return {
        "rows": rows,
        "insert_with_trigger_s": round(load_s, 1),
        "jobs": jobs,
        "claim_ms_before_analyze": cold,
        "claim_ms_after_analyze": warm,
    }


def bulk_disabled(url: str, rows: int) -> dict[str, Any]:
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(DECLARE)
        started = time.monotonic()
        conn.execute("SELECT bee.disable('doc', 'label')")
        conn.execute(FILL, (rows,))
        load_s = time.monotonic() - started
        started = time.monotonic()
        first_chunk = scalar(conn, "SELECT bee.enable('doc', 'label')")
        enable_ms = (time.monotonic() - started) * 1000
        first_claim_ms, claimed = claim_ms(conn)
    return {
        "rows": rows,
        "disabled_load_s": round(load_s, 1),
        "enable_ms": round(enable_ms, 1),
        "jobs_after_enable": first_chunk,
        "first_claim_ms": first_claim_ms,
        "claimed": claimed,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("case", choices=["backfill", "bulk-trigger", "bulk-disabled"])
    parser.add_argument("--rows", type=int, default=1_000_000)
    parser.add_argument("--out", type=Path, help="write the result as JSON")
    parser.add_argument(
        "--admin-url",
        default=os.environ.get("DATABASE_URL", "postgresql://aidb:aidb@localhost:4460/aidb"),
    )
    args = parser.parse_args()
    url = fresh_database(args.admin_url)
    case = {"backfill": backfill, "bulk-trigger": bulk_trigger, "bulk-disabled": bulk_disabled}
    result = {"case": args.case, **case[args.case](url, args.rows)}
    print(json.dumps(result, indent=2))
    if args.out:
        args.out.write_text(json.dumps(result, indent=2) + "\n")


if __name__ == "__main__":
    main()
