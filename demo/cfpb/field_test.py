"""Field test on real text: CFPB consumer complaints (demo/cfpb/sample.jsonl.gz).

Loads the complaints into a dedicated database, declares four derived columns and runs the real
worker process (`pgbee run`) until the queue is empty, then measures accuracy against the product
the consumer chose, confidence calibration, cost per 1000 rows, throughput and failures.

    cd worker && uv run python ../demo/cfpb/field_test.py --limit 60      # cost probe
    cd worker && uv run python ../demo/cfpb/field_test.py                 # all 3000 rows

It prints the estimate and asks before spending; --yes skips the question.

The gold label is noisy: consumers pick the product themselves and sometimes pick wrong.
Every run recreates the database aidb_cfpb. Results go to demo/cfpb/results[-N].json.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import DictRow, dict_row
from psycopg.types.json import Jsonb

from pgbee.installer import install
from pgbee.settings import load_settings

HERE = Path(__file__).parent
DB_NAME = "aidb_cfpb"
LLM_MODEL = "openai/gpt-6-luna"
LLM_CONFIG = {"reasoning": {"effort": "low"}}
DECISION_MODEL = "typesafe/jev-1.13"
EMBEDDING_MODEL = "openai/text-embedding-3-small"

PRODUCTS = {
    "credit_reporting": "Credit reports, credit scores and credit bureaus (Equifax, Experian, "
    "TransUnion): wrong information on a report, disputes, credit repair services",
    "debt_collection": "A debt collector or collection agency trying to collect a debt: calls, "
    "letters, debts not owed, collection practices",
    "bank_account": "Checking or savings accounts: deposits, withdrawals, opening or closing an "
    "account, overdraft fees, frozen accounts",
    "credit_card": "Credit cards or prepaid cards: charges, billing disputes, card fees, "
    "interest, rewards, managing the card account",
    "mortgage": "Home mortgages: servicing, escrow, payments, modification, foreclosure, "
    "refinancing",
    "money_transfer": "Money transfers and payment apps, wires, virtual currency, money orders, "
    "services like PayPal, Zelle, Cash App or Venmo",
    "vehicle_loan": "Auto loans or leases: vehicle financing, repossession, lease terms, car "
    "loan payments",
    "student_loan": "Student loans, federal or private: servicing, repayment plans, "
    "forgiveness, deferment",
    "personal_loan": "Payday loans, title loans, installment or personal loans, cash advances",
}
PRODUCT_PROMPT = (
    "Which financial product is this consumer complaint about? "
    "Pick the product the complaint is mainly about."
)
# Measured on the full run (results.json): 0.41 USD for 3000 rows and four columns.
COST_PER_ROW_USD = 0.000136
# Safety caps per column (USD, per month): far above the expected spend of a full run.
BUDGETS = {"product_llm": 1.0, "product_jev": 0.5, "money_lost": 0.5, "embedding": 0.2}


def admin_url(database_url: str) -> str:
    return database_url.rpartition("/")[0] + "/postgres"


def db_url(database_url: str) -> str:
    return database_url.rpartition("/")[0] + f"/{DB_NAME}"


def load_sample(limit: int | None) -> list[dict[str, Any]]:
    with gzip.open(HERE / "sample.jsonl.gz", "rt", encoding="utf-8") as f:
        rows = [json.loads(line) for line in f]
    return rows[:limit] if limit else rows


def prepare(url: str, rows: list[dict[str, Any]], base_url: str) -> None:
    with psycopg.connect(admin_url(base_url), autocommit=True) as admin:
        admin.execute(f"DROP DATABASE IF EXISTS {DB_NAME} WITH (FORCE)")
        admin.execute(f"CREATE DATABASE {DB_NAME}")
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        install(conn)
        conn.execute(
            "CREATE TABLE complaint (complaint_id bigint PRIMARY KEY, received date NOT NULL,"
            " product_gold text NOT NULL, issue text, narrative text NOT NULL)"
        )
        with (
            conn.cursor() as cur,
            cur.copy(
                "COPY complaint (complaint_id, received, product_gold, issue, narrative) FROM STDIN"
            ) as copy,
        ):
            for r in rows:
                copy.write_row(
                    (r["complaint_id"], r["received"], r["product"], r["issue"], r["narrative"])
                )
        declare(conn)


def declare(conn: psycopg.Connection[Any]) -> None:
    def config(column: str, **extra: Any) -> Jsonb:
        return Jsonb({"budget_usd": BUDGETS[column], "budget_period": "total", **extra})

    conn.execute(
        "SELECT bee.add_column('complaint', 'product_jev', array['narrative'], 'enum', 'decision',"
        " p_prompt => %s, p_model => %s, p_output_schema => %s, p_config => %s)",
        (PRODUCT_PROMPT, DECISION_MODEL, Jsonb(PRODUCTS), config("product_jev", concurrency=8)),
    )
    conn.execute(
        "SELECT bee.add_column('complaint', 'money_lost', array['narrative'], 'boolean',"
        " 'decision', p_prompt => %s, p_model => %s, p_config => %s)",
        (
            "Did the consumer lose money or get charged an amount they dispute?",
            DECISION_MODEL,
            config("money_lost", concurrency=8),
        ),
    )
    conn.execute(
        "SELECT bee.add_column('complaint', 'product_llm', array['narrative'], 'enum', 'llm',"
        " p_prompt => %s, p_model => %s, p_output_schema => %s, p_backend_config => %s,"
        " p_config => %s)",
        (
            PRODUCT_PROMPT,
            LLM_MODEL,
            Jsonb(PRODUCTS),
            Jsonb(LLM_CONFIG),
            config("product_llm", concurrency=8),
        ),
    )
    conn.execute(
        "SELECT bee.add_column('complaint', 'embedding', array['narrative'], 'vector',"
        " 'embedding', p_model => %s, p_output_schema => '{\"dimensions\": 1536}',"
        " p_backend_config => '{\"batch_size\": 100}', p_config => %s)",
        (EMBEDDING_MODEL, config("embedding")),
    )


def run_worker(url: str, total_jobs: int, timeout_s: float) -> float:
    """Run `pgbee run` as a real process until every job is done or dead. Returns seconds."""
    env = {**os.environ, "DATABASE_URL": url, "PGBEE_LOG_LEVEL": "warning"}
    started = time.monotonic()
    proc = subprocess.Popen(["pgbee", "run", "--batch-size", "50"], env=env, cwd=Path.cwd())
    try:
        with psycopg.connect(url, autocommit=True, row_factory=dict_row) as conn:
            while True:
                time.sleep(2)
                row = conn.execute(
                    "SELECT sum(pending) AS pending, sum(claimed) AS claimed, sum(done) AS done,"
                    " sum(dead) AS dead, bool_or(backfill_pending) AS scanning,"
                    " bool_or(b.exhausted) AS exhausted"
                    " FROM bee.columns c JOIN bee.budgets b ON b.column_def_id = c.id"
                ).fetchone()
                assert row is not None
                elapsed = time.monotonic() - started
                print(
                    f"\r  {elapsed:6.0f}s  done={row['done']}/{total_jobs} dead={row['dead']}"
                    f" pending={row['pending']} claimed={row['claimed']}",
                    end="",
                    flush=True,
                )
                idle = row["pending"] == 0 and row["claimed"] == 0 and not row["scanning"]
                if idle or row["exhausted"] or elapsed > timeout_s or proc.poll() is not None:
                    print()
                    if row["exhausted"]:
                        print("  a budget is exhausted: stopping")
                    return elapsed
    finally:
        proc.terminate()
        proc.wait(timeout=30)


def metrics(url: str, elapsed: float) -> dict[str, Any]:
    with psycopg.connect(url, row_factory=dict_row) as conn:
        out: dict[str, Any] = {"rows": scalar(conn, "SELECT count(*) FROM complaint")}
        out["elapsed_s"] = round(elapsed, 1)
        out["columns"] = conn.execute(
            "SELECT c.column_name, c.backend, c.model, c.done, c.dead,"
            " (SELECT coalesce(sum(j.attempts - 1), 0) FROM bee.job j"
            "   WHERE j.column_def_id = c.id AND j.status = 'done') AS retries,"
            " round(b.spent_total_usd, 5)::float AS cost_usd,"
            " round(1000 * b.spent_total_usd / nullif(c.done, 0), 4)::float AS cost_per_1000,"
            " (SELECT round(avg(r.latency_ms)) FROM bee.result r"
            "   WHERE r.column_def_id = c.id AND r.is_current)::int AS avg_latency_ms"
            " FROM bee.columns c JOIN bee.budgets b ON b.column_def_id = c.id ORDER BY c.id"
        ).fetchall()
        out["errors"] = conn.execute(
            "SELECT column_name, left(last_error, 160) AS error, count(*) AS n FROM bee.dead_jobs"
            " GROUP BY 1, 2 ORDER BY n DESC LIMIT 10"
        ).fetchall()
        out["accuracy"] = {
            col: scalar(
                conn,
                f"SELECT round(avg(({col} = product_gold)::int), 4)::float FROM complaint"
                f" WHERE {col} IS NOT NULL",
            )
            for col in ("product_jev", "product_llm")
        }
        out["agreement"] = conn.execute(
            "SELECT round(avg((product_jev = product_llm)::int), 4)::float AS agree,"
            " round(avg((product_jev = product_gold)::int) FILTER"
            "   (WHERE product_jev = product_llm), 4)::float AS accuracy_when_agree,"
            " count(*) FILTER (WHERE product_jev <> product_llm) AS disagreements"
            " FROM complaint WHERE product_jev IS NOT NULL AND product_llm IS NOT NULL"
        ).fetchone()
        out["calibration"] = {
            col: conn.execute(
                "SELECT CASE WHEN r.confidence < 0.5 THEN '<0.5' WHEN r.confidence < 0.7"
                " THEN '0.5-0.7' WHEN r.confidence < 0.9 THEN '0.7-0.9' ELSE '>=0.9' END AS bucket,"
                " count(*) AS n, round(avg((r.value #>> '{}' = c.product_gold)::int), 3)::float"
                " AS accuracy"
                " FROM bee.result r JOIN bee.column_def d ON d.id = r.column_def_id"
                " JOIN complaint c ON c.complaint_id = (r.row_pk ->> 'complaint_id')::bigint"
                " WHERE d.column_name = %s AND r.is_current GROUP BY 1 ORDER BY 1",
                (col,),
            ).fetchall()
            for col in ("product_jev", "product_llm")
        }
        out["per_class_accuracy"] = conn.execute(
            "SELECT product_gold, count(*) AS n,"
            " round(avg((product_jev = product_gold)::int), 3)::float AS jev,"
            " round(avg((product_llm = product_gold)::int), 3)::float AS llm"
            " FROM complaint GROUP BY 1 ORDER BY 1"
        ).fetchall()
        out["top_confusions"] = conn.execute(
            "SELECT product_gold AS gold, product_llm AS predicted, count(*) AS n FROM complaint"
            " WHERE product_llm <> product_gold GROUP BY 1, 2 ORDER BY n DESC LIMIT 8"
        ).fetchall()
        out["decision_questions_per_call"] = scalar(
            conn,
            "SELECT round(avg((details ->> 'questions_in_call')::numeric), 2)::float"
            " FROM bee.result WHERE details ? 'questions_in_call'",
        )
        out["money_lost_true_rate"] = scalar(
            conn, "SELECT round(avg(money_lost::int), 3)::float FROM complaint"
        )
    return out


def scalar(conn: psycopg.Connection[DictRow], sql: str) -> Any:
    row = conn.execute(sql).fetchone()
    assert row is not None
    return next(iter(row.values()))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, help="Use only the first N complaints.")
    parser.add_argument("--timeout", type=float, default=3600, help="Seconds before giving up.")
    parser.add_argument("--yes", action="store_true", help="Do not ask before spending.")
    args = parser.parse_args()
    settings = load_settings()
    if not settings.openrouter_api_key:
        sys.exit("OPENROUTER_API_KEY is not set")
    rows = load_sample(args.limit)
    estimate = len(rows) * COST_PER_ROW_USD
    print(
        f"Estimate: {len(rows)} complaints x 4 columns, about {estimate:.2f} USD"
        f" (measured {COST_PER_ROW_USD * 1000:.3f} USD per 1000 rows), capped by the column budgets"
    )
    if not args.yes and input("Proceed? [y/N] ").strip().lower() != "y":
        sys.exit("not run")
    url = db_url(settings.database_url)
    print(f"== {len(rows)} complaints into {DB_NAME}, four derived columns")
    prepare(url, rows, settings.database_url)
    print("== worker")
    elapsed = run_worker(url, total_jobs=4 * len(rows), timeout_s=args.timeout)
    result = metrics(url, elapsed)
    suffix = f"-{args.limit}" if args.limit else ""
    out = HERE / f"results{suffix}.json"
    out.write_text(json.dumps(result, indent=2, default=str) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2, default=str))
    print(f"== written {out}")


if __name__ == "__main__":
    main()
