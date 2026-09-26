# ruff: noqa: E501
"""Compare models on the demo's urgency and category columns, using the extension's own versioning.

Each model becomes a new version of the two columns; the worker recomputes every row; accuracy is
measured against demo/gold.json from ai.result, cost and latency from ai.cost_by_column.

    cd worker && uv run python ../demo/compare_models.py [--yes] [--models a,b,c]

Requires the demo to have run (ticket table with urgency and category declared).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import DictRow, dict_row
from psycopg.types.json import Jsonb

from aicol.db import Contract
from aicol.providers import OpenRouterProvider
from aicol.settings import load_settings
from aicol.worker import Worker

HERE = Path(__file__).resolve().parent
COLUMNS = ("urgency", "category")

# (OpenRouter id, backend_config). Reasoning kept off or low: row classification wants latency, not thinking.
CANDIDATES: list[tuple[str, dict[str, Any]]] = [
    ("deepseek/deepseek-v4.1-flash", {"reasoning": {"enabled": False}}),
    ("google/gemini-3.7-flash", {"reasoning": {"effort": "low"}}),
    ("z-ai/glm-5.3-flash", {"reasoning": {"effort": "low"}}),
    ("xiaomi/mimo-v2.6-pro", {"reasoning": {"enabled": False}}),
    ("openai/gpt-6-luna", {"reasoning": {"effort": "low"}}),
    ("mistralai/ministral-14b-2512", {}),
    ("anthropic/claude-haiku-4.5", {}),
]


def load_gold() -> dict[str, dict[str, set[str]]]:
    raw = json.loads((HERE / "gold.json").read_text(encoding="utf-8"))
    gold: dict[str, dict[str, set[str]]] = {}
    for column in COLUMNS:
        gold[column] = {k: ({v} if isinstance(v, str) else set(v)) for k, v in raw[column].items()}
    return gold


async def drain(worker: Worker, conn: psycopg.Connection[DictRow]) -> int:
    """Run batches until the queue is empty, waiting out jobs in backoff (a few seconds at most)."""
    total = 0
    while True:
        stats = await worker.run_once()
        total += stats.claimed
        if stats.rate_limited:
            await asyncio.sleep(5)
            continue
        if stats.claimed:
            continue
        waiting = conn.execute(
            "SELECT extract(epoch FROM min(next_attempt_at) - now()) AS s FROM ai.job WHERE status = 'pending'"
        ).fetchone()
        if waiting is None or waiting["s"] is None:
            return total
        await asyncio.sleep(min(max(float(waiting["s"]), 0.5), 90))


def new_version(
    conn: psycopg.Connection[DictRow], column: str, model: str, cfg: dict[str, Any]
) -> int:
    try:
        row = conn.execute(
            "SELECT ai.update_column('ticket', %s, p_model => %s, p_backend_config => %s) AS v",
            (column, model, Jsonb(cfg)),
        ).fetchone()
        assert row is not None
        return int(row["v"])
    except psycopg.errors.RaiseException as exc:
        if "nothing changed" not in str(exc):
            raise
        row = conn.execute(
            "SELECT id, current_version_id AS v FROM ai.column_def WHERE table_name = 'ticket' AND column_name = %s AND deleted_at IS NULL",
            (column,),
        ).fetchone()
        assert row is not None
        conn.execute("SELECT ai.backfill(%s)", (row["id"],))
        return int(row["v"])


def score(
    conn: psycopg.Connection[DictRow], column: str, version_id: int, gold: dict[str, set[str]]
) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT row_pk ->> 'id' AS id, value, confidence FROM ai.result"
        " WHERE column_version_id = %s AND source = 'model' AND is_current",
        (version_id,),
    ).fetchall()
    predicted = {r["id"]: r["value"] for r in rows}
    hits = sum(1 for k, acceptable in gold.items() if predicted.get(k) in acceptable)
    misses = [
        f"{k}:{predicted.get(k, '∅')}≠{'/'.join(sorted(acceptable))}"
        for k, acceptable in gold.items()
        if predicted.get(k) not in acceptable
    ]
    cost = conn.execute(
        "SELECT results, prompt_tokens, completion_tokens, cost, avg_latency_ms FROM ai.cost_by_column WHERE column_version_id = %s",
        (version_id,),
    ).fetchone()
    dead = conn.execute(
        "SELECT count(*) AS n FROM ai.job j JOIN ai.column_def d ON d.id = j.column_def_id"
        " WHERE d.column_name = %s AND j.status = 'dead'",
        (column,),
    ).fetchone()
    return {
        "column": column,
        "answered": len(predicted),
        "accuracy": hits / len(gold),
        "misses": misses,
        "cost": float(cost["cost"]) if cost and cost["cost"] is not None else 0.0,
        "latency_ms": cost["avg_latency_ms"] if cost else None,
        "completion_tokens": cost["completion_tokens"] if cost else None,
        "dead": dead["n"] if dead else 0,
    }


async def run(models: list[tuple[str, dict[str, Any]]]) -> None:
    settings = load_settings()
    assert settings.openrouter_api_key is not None
    conn = psycopg.connect(settings.database_url, autocommit=True, row_factory=dict_row)
    gold = load_gold()
    conn.execute("SELECT ai.unpin('ticket', 'urgency', '{\"id\": 12}')")
    conn.execute("DELETE FROM ai.job WHERE status = 'dead'")

    contract = await Contract.connect(settings.database_url)
    provider = OpenRouterProvider(settings.openrouter_api_key, settings.openrouter_base_url)
    worker = Worker(contract, provider, worker_id="compare", batch_size=50)

    results: list[dict[str, Any]] = []
    for model, cfg in models:
        print(f"\n== {model} {cfg or ''}")
        versions = {c: new_version(conn, c, model, cfg) for c in COLUMNS}
        n = await drain(worker, conn)
        print(f"  {n} job elaborati")
        for c in COLUMNS:
            s = score(conn, c, versions[c], gold[c])
            s["model"] = model
            results.append(s)
            print(
                f"  {c:9} acc={s['accuracy']:.0%} answered={s['answered']}/{len(gold[c])} dead={s['dead']}"
                f" cost={s['cost']:.5f} latency={s['latency_ms']}ms out_tokens={s['completion_tokens']}"
            )
            if s["misses"]:
                print("           miss: " + ", ".join(s["misses"]))
        conn.execute("DELETE FROM ai.job WHERE status = 'dead'")

    await contract.close()

    print("\n== Riepilogo (accuratezza media sulle due colonne, costo per 42 righe)")
    print(f"  {'modello':34} {'acc':>5} {'urg':>5} {'cat':>5} {'USD':>8} {'ms':>6} {'dead':>4}")
    by_model: dict[str, list[dict[str, Any]]] = {}
    for r in results:
        by_model.setdefault(r["model"], []).append(r)
    for model, rs in sorted(by_model.items(), key=lambda kv: -sum(r["accuracy"] for r in kv[1])):
        acc = sum(r["accuracy"] for r in rs) / len(rs)
        urg = next(r["accuracy"] for r in rs if r["column"] == "urgency")
        cat = next(r["accuracy"] for r in rs if r["column"] == "category")
        cost = sum(r["cost"] for r in rs)
        lat = sum(r["latency_ms"] or 0 for r in rs) / len(rs)
        dead = sum(r["dead"] for r in rs)
        print(f"  {model:34} {acc:5.0%} {urg:5.0%} {cat:5.0%} {cost:8.5f} {lat:6.0f} {dead:4}")
    (HERE / "results").mkdir(exist_ok=True)
    (HERE / "results" / "models.json").write_text(json.dumps(results, indent=2, ensure_ascii=False))
    conn.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--models", help="comma separated OpenRouter ids, subset of the candidates")
    args = parser.parse_args()
    models = CANDIDATES
    if args.models:
        wanted = set(args.models.split(","))
        models = [m for m in CANDIDATES if m[0] in wanted]
    calls = len(models) * 42
    print(
        f"Stima: {len(models)} modelli × 42 chiamate = {calls} chiamate, sotto {calls * 0.001:.2f} USD"
    )
    if not args.yes and input("Procedo? [s/N] ").strip().lower() not in {"s", "si", "sì", "y"}:
        sys.exit("annullato")
    asyncio.run(run(models))


if __name__ == "__main__":
    main()
