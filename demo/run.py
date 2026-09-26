# ruff: noqa: E501
"""End-to-end demo: derived columns on a support-ticket table, driven by the reference worker.

Run from the worker directory so the aicol package is importable:

    cd worker && uv run python ../demo/run.py [--skip-embedding] [--yes]

Costs money (OpenRouter). The script prints the estimate and asks before the first call.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import DictRow, dict_row
from psycopg.types.json import Jsonb

from aicol.db import Contract
from aicol.installer import install
from aicol.providers import OpenRouterProvider
from aicol.settings import load_settings
from aicol.worker import Worker

HERE = Path(__file__).resolve().parent
LLM_MODEL = "anthropic/claude-haiku-4.5"
EMBEDDING_MODEL = "openai/text-embedding-3-small"
EMBEDDING_DIMENSIONS = 1536

CATEGORIES = ["fatturazione", "tecnico", "account", "commerciale", "altro"]
URGENCIES = ["low", "medium", "high"]
EXTRACTED_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "requested_action": {"type": "string", "description": "What the customer wants done"},
        "mentions_money": {"type": "boolean"},
        "references": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Invoice numbers, transaction ids, domains, names quoted in the text",
        },
    },
    "required": ["requested_action", "mentions_money", "references"],
    "additionalProperties": False,
}


def say(title: str) -> None:
    print(f"\n== {title}")


def show(conn: psycopg.Connection[DictRow], sql: str, params: Any = None) -> None:
    rows = conn.execute(sql, params).fetchall()
    if not rows:
        print("  (nessuna riga)")
        return
    cols = list(rows[0].keys())
    widths = {c: min(60, max(len(c), *(len(_cell(r[c])) for r in rows))) for c in cols}
    print("  " + "  ".join(c.ljust(widths[c]) for c in cols))
    for r in rows:
        print("  " + "  ".join(_cell(r[c])[: widths[c]].ljust(widths[c]) for c in cols))


def _cell(v: Any) -> str:
    if v is None:
        return "·"
    s = str(v).replace("\n", " ")
    return s


def declare_columns(conn: psycopg.Connection[DictRow], with_embedding: bool) -> None:
    conn.execute(
        "SELECT ai.add_column('ticket', 'urgency', array['body'], 'enum',"
        " p_prompt => %s, p_model => %s, p_output_schema => %s::jsonb,"
        " p_config => '{\"confidence_threshold\": 0.75}')",
        (
            "Classifica l'urgenza del ticket di assistenza. high: il negozio non vende o c'è un "
            "problema di privacy o denaro. medium: un malfunzionamento che non blocca le vendite. "
            "low: domande, richieste amministrative, complimenti.",
            LLM_MODEL,
            Jsonb(URGENCIES),
        ),
    )
    conn.execute(
        "SELECT ai.add_column('ticket', 'category', array['body'], 'enum',"
        " p_prompt => %s, p_model => %s, p_output_schema => %s::jsonb)",
        (
            "Assegna la categoria del ticket: fatturazione (fatture, pagamenti, canoni, note di "
            "credito), tecnico (errori, bug, integrazioni, DNS, prestazioni), account (accessi, "
            "utenti, permessi, disdette), commerciale (piani, preventivi, funzionalità incluse), "
            "altro.",
            LLM_MODEL,
            Jsonb(CATEGORIES),
        ),
    )
    conn.execute(
        "SELECT ai.add_column('ticket', 'summary', array['customer', 'body'], 'text',"
        " p_prompt => %s, p_model => %s, p_output_schema => '{\"max_length\": 120}')",
        (
            "Riassumi il ticket in una frase in italiano, al massimo 120 caratteri, che un "
            "operatore possa leggere in coda.",
            LLM_MODEL,
        ),
    )
    conn.execute(
        "SELECT ai.add_column('ticket', 'extracted', array['body'], 'jsonb',"
        " p_prompt => %s, p_model => %s, p_output_schema => %s::jsonb)",
        (
            "Estrai dal ticket l'azione richiesta dal cliente, se parla di denaro, e i "
            "riferimenti citati (numeri di fattura, codici, domini, nomi).",
            LLM_MODEL,
            Jsonb(EXTRACTED_SCHEMA),
        ),
    )
    if with_embedding:
        conn.execute(
            "SELECT ai.add_column('ticket', 'embedding', array['body'], 'vector',"
            " p_backend => 'embedding', p_model => %s, p_output_schema => %s::jsonb)",
            (EMBEDDING_MODEL, Jsonb({"dimensions": EMBEDDING_DIMENSIONS})),
        )


async def drain(worker: Worker, label: str) -> None:
    total = 0
    while True:
        stats = await worker.run_once()
        total += stats.claimed
        if stats.rate_limited:
            await asyncio.sleep(5)
            continue
        if stats.claimed == 0:
            break
    print(f"  worker: {label}, {total} job elaborati")


def cost_report(conn: psycopg.Connection[DictRow]) -> None:
    show(
        conn,
        "SELECT column_name, column_version_id AS ver, model, results, prompt_tokens, completion_tokens,"
        " round(cost, 5) AS cost_usd, avg_latency_ms FROM ai.cost_by_column ORDER BY column_name, ver",
    )
    total = conn.execute("SELECT coalesce(sum(cost), 0) AS c FROM ai.cost_by_column").fetchone()
    assert total is not None
    print(f"  totale speso: {float(total['c']):.5f} USD")


N_LLM_CALLS = 4 * 20 + 4 * 1 + 20  # four columns, one new ticket, one recompute of urgency


def estimate(with_embedding: bool) -> str:
    usd = N_LLM_CALLS * (400 * 1e-6 + 60 * 5e-6)
    text = (
        f"Stima: circa {N_LLM_CALLS} chiamate a {LLM_MODEL} (~400 token in, ~60 out ciascuna)"
        f" ≈ {usd:.3f} USD"
    )
    if with_embedding:
        text += f", più 21 embedding con {EMBEDDING_MODEL} (trascurabili)"
    return text


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-embedding", action="store_true")
    parser.add_argument("--yes", action="store_true", help="Do not ask before spending")
    parser.add_argument(
        "--reset", action="store_true", help="Drop the ticket table and lineage first"
    )
    args = parser.parse_args()
    settings = load_settings()
    if not settings.openrouter_api_key:
        sys.exit("OPENROUTER_API_KEY non impostata (worker/.env)")
    print(estimate(not args.skip_embedding))
    if not args.yes and input("Procedo? [s/N] ").strip().lower() not in {"s", "si", "sì", "y"}:
        sys.exit("annullato")
    asyncio.run(run(args))


async def run(args: argparse.Namespace) -> None:
    settings = load_settings()

    conn = psycopg.connect(settings.database_url, autocommit=True, row_factory=dict_row)
    install(conn)
    conn.execute("CREATE EXTENSION IF NOT EXISTS vector")

    if args.reset:
        for col in ("urgency", "category", "summary", "extracted", "embedding"):
            try:
                conn.execute("SELECT ai.drop_column('ticket', %s)", (col,))
            except psycopg.Error:
                pass
        conn.execute("DROP TABLE IF EXISTS ticket")
        conn.execute("DELETE FROM ai.job")
        conn.execute("DELETE FROM ai.result")
    if conn.execute("SELECT to_regclass('ticket') AS t").fetchone()["t"] is not None:  # type: ignore[index]
        sys.exit("La tabella ticket esiste già: rilancia con --reset")

    say("1. Tabella ticket con 20 richieste di assistenza, nessuna colonna derivata")
    conn.execute((HERE / "seed.sql").read_text(encoding="utf-8"))
    show(
        conn, "SELECT id, customer, channel, left(body, 70) AS body FROM ticket ORDER BY id LIMIT 6"
    )

    say("2. Dichiarazione delle colonne derivate: una chiamata per colonna, poi non si tocca più")
    declare_columns(conn, with_embedding=not args.skip_embedding)
    show(
        conn, "SELECT column_name, backend, model, output_type, pending FROM ai.columns ORDER BY id"
    )

    contract = await Contract.connect(settings.database_url)
    assert settings.openrouter_api_key is not None
    provider = OpenRouterProvider(settings.openrouter_api_key, settings.openrouter_base_url)
    worker = Worker(contract, provider, worker_id="demo", batch_size=40)

    say("3. Il worker svuota la coda")
    await drain(worker, "primo passaggio")
    show(
        conn,
        "SELECT id, urgency, category, left(summary, 60) AS summary FROM ticket ORDER BY id",
    )
    show(
        conn,
        "SELECT id, extracted ->> 'requested_action' AS requested_action,"
        " extracted -> 'references' AS refs FROM ticket ORDER BY id LIMIT 6",
    )
    say("   Righe sotto la soglia di confidenza (0.75) per la colonna urgency")
    show(
        conn,
        "SELECT row_pk ->> 'id' AS id, value, confidence FROM ai.needs_review WHERE column_name = 'urgency' ORDER BY confidence",
    )

    say("4. Arriva un ticket nuovo: il trigger accoda, il worker lo prende")
    conn.execute(
        "INSERT INTO ticket (customer, channel, body) VALUES ('Macelleria Toro', 'chat',"
        " 'Il POS virtuale rifiuta tutte le carte da mezz''ora, in negozio c''è la fila')"
    )
    show(conn, "SELECT id, status, column_def_id FROM ai.job WHERE status = 'pending' ORDER BY id")
    await drain(worker, "ticket nuovo")
    show(conn, "SELECT id, urgency, category, summary FROM ticket WHERE id = 21")

    say("5. Un operatore corregge a mano: il valore resta, il modello non lo tocca più")
    conn.execute("UPDATE ticket SET urgency = 'low' WHERE id = 10")
    show(
        conn,
        "SELECT row_pk ->> 'id' AS id, source, value FROM ai.result WHERE is_current AND row_pk = '{\"id\": 10}' AND column_def_id = (SELECT id FROM ai.columns WHERE column_name = 'urgency')",
    )

    say("6. Cambia il prompt di urgency: versione 2, si ricalcolano solo le righe della versione 1")
    conn.execute(
        "SELECT ai.update_column('ticket', 'urgency', p_prompt => %s)",
        (
            "Classifica l'urgenza. high solo se il negozio non riesce a vendere o c'è un rischio "
            "legale o di privacy. medium per malfunzionamenti che degradano il servizio. low per "
            "tutto il resto, comprese le richieste amministrative anche se il cliente scrive URGENTE.",
        ),
    )
    show(
        conn,
        "SELECT column_name, version, stale, pending, human_overrides FROM ai.columns WHERE column_name = 'urgency'",
    )
    await drain(worker, "ricalcolo selettivo")
    show(
        conn,
        "SELECT t.id, t.urgency, r.column_version_id AS ver, r.source FROM ticket t"
        " JOIN ai.result r ON r.row_pk = jsonb_build_object('id', t.id) AND r.is_current"
        " AND r.column_def_id = (SELECT id FROM ai.columns WHERE column_name = 'urgency')"
        " ORDER BY t.id",
    )
    print("  (la riga 12 è rimasta 'high' con source = human e nessuna versione)")

    if not args.skip_embedding:
        say("7. Ticket simili a quello nuovo, con l'embedding mantenuto dalla stessa coda")
        show(
            conn,
            "SELECT id, customer, left(body, 60) AS body, round((embedding <=> (SELECT embedding FROM ticket WHERE id = 21))::numeric, 3) AS distance"
            " FROM ticket WHERE id <> 21 AND embedding IS NOT NULL ORDER BY embedding <=> (SELECT embedding FROM ticket WHERE id = 21) LIMIT 3",
        )

    say("8. Lineage e costi per colonna e versione")
    cost_report(conn)
    show(
        conn,
        "SELECT column_name, version, pending, claimed, done, dead, stale, human_overrides FROM ai.columns ORDER BY id",
    )

    await contract.close()
    conn.close()


if __name__ == "__main__":
    main()
