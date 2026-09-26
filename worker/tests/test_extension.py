"""Integration tests for the SQL extension. The worker is simulated by hand: no model involved."""

from __future__ import annotations

import json
from typing import Any

import psycopg
import pytest
from psycopg.rows import DictRow

URGENCY = json.dumps(["low", "medium", "high"])


def add_urgency(conn: psycopg.Connection[DictRow], **config: Any) -> int:
    row = conn.execute(
        "SELECT bee.add_column('ticket', 'urgency', array['body'], 'enum',"
        " p_prompt => 'Classifica l''urgenza', p_model => 'anthropic/claude-haiku-4.5',"
        " p_output_schema => %s::jsonb, p_config => %s::jsonb) AS id",
        (URGENCY, json.dumps(config)),
    ).fetchone()
    assert row is not None
    return int(row["id"])


def claim(
    conn: psycopg.Connection[DictRow], n: int = 10, worker: str = "w1"
) -> list[dict[str, Any]]:
    return conn.execute(
        "SELECT * FROM bee.claim_jobs(%s, %s) ORDER BY job_id", (worker, n)
    ).fetchall()


def complete(
    conn: psycopg.Connection[DictRow],
    job: dict[str, Any],
    value: Any,
    confidence: float | None = 0.9,
) -> str:
    row = conn.execute(
        "SELECT bee.complete_job(%s::bigint, %s, %s::jsonb, %s::real, 'anthropic/claude-haiku-4.5',"
        ' \'{"prompt_tokens": 100, "completion_tokens": 5, "cost": 0.0001}\'::jsonb, 320) AS outcome',
        (job["job_id"], job["source_hash"], json.dumps(value), confidence),
    ).fetchone()
    assert row is not None
    return str(row["outcome"])


def jobs(conn: psycopg.Connection[DictRow], status: str | None = None) -> list[dict[str, Any]]:
    if status:
        return conn.execute(
            "SELECT * FROM bee.job WHERE status = %s ORDER BY id", (status,)
        ).fetchall()
    return conn.execute("SELECT * FROM bee.job ORDER BY id").fetchall()


def urgency_of(conn: psycopg.Connection[DictRow], ticket_id: int) -> str | None:
    row = conn.execute("SELECT urgency FROM ticket WHERE id = %s", (ticket_id,)).fetchone()
    assert row is not None
    value = row["urgency"]
    return None if value is None else str(value)


def scalar(conn: psycopg.Connection[DictRow], sql: str) -> Any:
    row = conn.execute(sql).fetchone()
    assert row is not None
    return next(iter(row.values()))


def test_add_column_creates_column_triggers_and_backfills(
    conn: psycopg.Connection[DictRow], ticket: str
) -> None:
    def_id = add_urgency(conn)
    col = conn.execute(
        "SELECT data_type FROM information_schema.columns WHERE table_name = 'ticket' AND column_name = 'urgency'"
    ).fetchone()
    assert col is not None and col["data_type"] == "text"
    triggers = conn.execute(
        "SELECT tgname FROM pg_trigger WHERE tgrelid = 'ticket'::regclass AND NOT tgisinternal ORDER BY tgname"
    ).fetchall()
    assert [t["tgname"] for t in triggers] == ["bee_enqueue_urgency", "bee_override_urgency"]
    assert len(jobs(conn, "pending")) == 3
    view = conn.execute("SELECT * FROM bee.columns").fetchone()
    assert view is not None
    assert view["id"] == def_id and view["version"] == 1 and view["pending"] == 3
    assert view["config"]["max_attempts"] == 5


def test_add_column_rejects_bad_definitions(conn: psycopg.Connection[DictRow], ticket: str) -> None:
    with pytest.raises(psycopg.errors.UndefinedColumn):
        conn.execute(
            "SELECT bee.add_column('ticket', 'x', array['nope'], 'text', p_prompt => 'p', p_model => 'm')"
        )
    with pytest.raises(psycopg.errors.RaiseException, match="requires a prompt"):
        conn.execute("SELECT bee.add_column('ticket', 'x', array['body'], 'text', p_model => 'm')")
    with pytest.raises(psycopg.errors.RaiseException, match="enum requires output_schema"):
        conn.execute(
            "SELECT bee.add_column('ticket', 'x', array['body'], 'enum', p_prompt => 'p', p_model => 'm')"
        )
    with pytest.raises(psycopg.errors.RaiseException, match="cannot produce a vector"):
        conn.execute(
            "SELECT bee.add_column('ticket', 'x', array['body'], 'vector', p_prompt => 'p', p_model => 'm',"
            " p_output_schema => '{\"dimensions\": 3}')"
        )
    conn.execute("CREATE TABLE nopk (body text)")
    with pytest.raises(psycopg.errors.RaiseException, match="no primary key"):
        conn.execute(
            "SELECT bee.add_column('nopk', 'x', array['body'], 'text', p_prompt => 'p', p_model => 'm')"
        )


def test_insert_claim_complete_writes_value_and_lineage(
    conn: psycopg.Connection[DictRow], ticket: str
) -> None:
    add_urgency(conn)
    conn.execute("DELETE FROM bee.job")
    conn.execute("INSERT INTO ticket (body) VALUES ('Fattura sbagliata, urgente')")
    pending = jobs(conn, "pending")
    assert len(pending) == 1
    claimed = claim(conn)
    assert len(claimed) == 1
    job = claimed[0]
    assert job["source"] == {"body": "Fattura sbagliata, urgente"}
    assert job["prompt"] == "Classifica l'urgenza"
    assert job["backend"] == "llm" and job["output_type"] == "enum"
    assert (
        json.loads(job["output_schema"])
        if isinstance(job["output_schema"], str)
        else job["output_schema"] == ["low", "medium", "high"]
    )
    assert job["attempts"] == 1
    assert jobs(conn, "claimed")[0]["claimed_by"] == "w1"

    assert complete(conn, job, "high") == "written"
    ticket_id = job["row_pk"]["id"]
    assert urgency_of(conn, ticket_id) == "high"
    result = conn.execute("SELECT * FROM bee.result WHERE is_current").fetchone()
    assert result is not None
    assert result["source"] == "model" and result["value"] == "high" and result["written"] is True
    assert result["column_version_id"] is not None and result["confidence"] == pytest.approx(0.9)
    assert jobs(conn, "done")[0]["id"] == job["job_id"]
    cost = conn.execute("SELECT * FROM bee.cost_by_column").fetchone()
    assert (
        cost is not None and cost["results"] == 1 and float(cost["cost"]) == pytest.approx(0.0001)
    )


def test_source_change_enqueues_unrelated_change_does_not(
    conn: psycopg.Connection[DictRow], ticket: str
) -> None:
    add_urgency(conn)
    for job in claim(conn):
        complete(conn, job, "low")
    assert jobs(conn, "pending") == []
    conn.execute("UPDATE ticket SET customer = 'Delta' WHERE id = 1")
    assert jobs(conn, "pending") == []
    conn.execute("UPDATE ticket SET body = body WHERE id = 1")
    assert jobs(conn, "pending") == []
    conn.execute("UPDATE ticket SET body = 'Testo nuovo' WHERE id = 1")
    pending = jobs(conn, "pending")
    assert len(pending) == 1 and pending[0]["row_pk"] == {"id": 1}


def test_row_changed_during_compute_is_requeued(
    conn: psycopg.Connection[DictRow], ticket: str
) -> None:
    add_urgency(conn)
    conn.execute("DELETE FROM bee.job WHERE row_pk <> '{\"id\": 1}'::jsonb")
    job = claim(conn)[0]
    conn.execute("UPDATE ticket SET body = 'Cambiato mentre il worker lavora' WHERE id = 1")
    live = jobs(conn)
    assert len(live) == 1 and live[0]["status"] == "claimed"
    assert bytes(live[0]["source_hash"]) != bytes(job["source_hash"])
    assert complete(conn, job, "high") == "stale_requeued"
    assert urgency_of(conn, 1) is None
    assert jobs(conn, "pending")[0]["id"] == job["job_id"]
    history = conn.execute("SELECT is_current, written FROM bee.result").fetchall()
    assert history == [{"is_current": False, "written": False}]
    job2 = claim(conn)[0]
    assert job2["source"] == {"body": "Cambiato mentre il worker lavora"}
    assert complete(conn, job2, "medium") == "written"
    assert urgency_of(conn, 1) == "medium"


def test_fail_job_backs_off_then_dies(conn: psycopg.Connection[DictRow], ticket: str) -> None:
    add_urgency(conn, max_attempts=2, backoff_base_seconds=60)
    conn.execute("DELETE FROM bee.job WHERE row_pk <> '{\"id\": 1}'::jsonb")
    job = claim(conn)[0]
    status = conn.execute(
        "SELECT bee.fail_job(%s, 'HTTP 429', true) AS s", (job["job_id"],)
    ).fetchone()
    assert status is not None and status["s"] == "pending"
    j = jobs(conn)[0]
    assert j["last_error"] == "HTTP 429" and j["attempts"] == 1
    delay = conn.execute("SELECT next_attempt_at - now() AS d FROM bee.job").fetchone()
    assert delay is not None and 55 <= delay["d"].total_seconds() <= 60
    assert claim(conn) == []
    conn.execute("UPDATE bee.job SET next_attempt_at = now()")
    job = claim(conn)[0]
    assert job["attempts"] == 2
    status = conn.execute(
        "SELECT bee.fail_job(%s, 'HTTP 500', true) AS s", (job["job_id"],)
    ).fetchone()
    assert status is not None and status["s"] == "dead"
    dead = conn.execute("SELECT * FROM bee.dead_jobs").fetchall()
    assert len(dead) == 1 and dead[0]["last_error"] == "HTTP 500" and dead[0]["row_pk"] == {"id": 1}
    retried = conn.execute("SELECT bee.retry_dead('ticket', 'urgency') AS n").fetchone()
    assert retried is not None and retried["n"] == 1
    assert jobs(conn, "pending")[0]["attempts"] == 0


def test_non_retryable_failure_dies_immediately(
    conn: psycopg.Connection[DictRow], ticket: str
) -> None:
    add_urgency(conn)
    job = claim(conn, 1)[0]
    status = conn.execute(
        "SELECT bee.fail_job(%s, 'schema mismatch', false) AS s", (job["job_id"],)
    ).fetchone()
    assert status is not None and status["s"] == "dead"


def test_update_column_recomputes_only_stale_rows(
    conn: psycopg.Connection[DictRow], ticket: str
) -> None:
    add_urgency(conn)
    for job in claim(conn):
        complete(conn, job, "low")
    conn.execute("UPDATE ticket SET urgency = 'high' WHERE id = 3")
    new_version = conn.execute(
        "SELECT bee.update_column('ticket', 'urgency', p_prompt => 'Classifica meglio') AS v"
    ).fetchone()
    assert new_version is not None
    view = conn.execute(
        "SELECT version, prompt, stale, pending, human_overrides FROM bee.columns"
    ).fetchone()
    assert view is not None
    assert view["version"] == 2 and view["prompt"] == "Classifica meglio"
    assert view["stale"] == 2 and view["pending"] == 2 and view["human_overrides"] == 1
    stale = conn.execute("SELECT row_pk FROM bee.stale_rows ORDER BY row_pk").fetchall()
    assert [s["row_pk"] for s in stale] == [{"id": 1}, {"id": 2}]
    for job in claim(conn):
        assert job["prompt"] == "Classifica meglio"
        complete(conn, job, "medium")
    assert scalar(conn, "SELECT count(*) AS n FROM bee.stale_rows") == 0
    assert urgency_of(conn, 3) == "high"
    with pytest.raises(psycopg.errors.RaiseException, match="nothing changed"):
        conn.execute(
            "SELECT bee.update_column('ticket', 'urgency', p_prompt => 'Classifica meglio')"
        )


def test_update_column_resets_backoff_of_pending_jobs(
    conn: psycopg.Connection[DictRow], ticket: str
) -> None:
    add_urgency(conn, backoff_base_seconds=600)
    job = claim(conn, 1)[0]
    conn.execute("SELECT bee.fail_job(%s, 'model returned invalid JSON', true)", (job["job_id"],))
    waiting = jobs(conn, "pending")
    assert waiting[0]["attempts"] == 1 and waiting[0]["last_error"] is not None
    assert claim(conn, 1)[0]["job_id"] != job["job_id"]
    conn.execute(
        "SELECT bee.update_column('ticket', 'urgency', p_model => 'anthropic/claude-sonnet-4.5')"
    )
    row = conn.execute(
        "SELECT attempts, last_error, next_attempt_at <= now() AS ready FROM bee.job WHERE id = %s",
        (job["job_id"],),
    ).fetchone()
    assert row == {"attempts": 0, "last_error": None, "ready": True}


def test_human_override_pins_the_row(conn: psycopg.Connection[DictRow], ticket: str) -> None:
    add_urgency(conn)
    for job in claim(conn):
        complete(conn, job, "low")
    conn.execute("UPDATE ticket SET urgency = 'high' WHERE id = 1")
    current = conn.execute(
        "SELECT source, value, column_version_id FROM bee.result WHERE is_current AND row_pk = '{\"id\": 1}'"
    ).fetchone()
    assert current == {"source": "human", "value": "high", "column_version_id": None}
    conn.execute("UPDATE ticket SET body = 'Testo cambiato' WHERE id = 1")
    assert jobs(conn, "pending") == []
    conn.execute(
        "SELECT bee.update_column('ticket', 'urgency', p_model => 'anthropic/claude-sonnet-4.5')"
    )
    assert sorted(j["row_pk"]["id"] for j in jobs(conn, "pending")) == [2, 3]

    unpinned = conn.execute("SELECT bee.unpin('ticket', 'urgency', '{\"id\": 1}') AS ok").fetchone()
    assert unpinned is not None and unpinned["ok"] is True
    assert {"id": 1} in [j["row_pk"] for j in jobs(conn, "pending")]
    again = conn.execute("SELECT bee.unpin('ticket', 'urgency', '{\"id\": 1}') AS ok").fetchone()
    assert again is not None and again["ok"] is False


def test_human_override_cancels_live_job_and_worker_result_is_discarded(
    conn: psycopg.Connection[DictRow], ticket: str
) -> None:
    add_urgency(conn)
    conn.execute("DELETE FROM bee.job WHERE row_pk <> '{\"id\": 1}'::jsonb")
    job = claim(conn)[0]
    conn.execute("UPDATE ticket SET urgency = 'medium' WHERE id = 1")
    assert jobs(conn) == []
    assert complete(conn, job, "high") == "cancelled"
    assert urgency_of(conn, 1) == "medium"


def test_setting_target_null_asks_for_recompute(
    conn: psycopg.Connection[DictRow], ticket: str
) -> None:
    add_urgency(conn)
    for job in claim(conn):
        complete(conn, job, "low")
    conn.execute("UPDATE ticket SET urgency = 'high' WHERE id = 2")
    conn.execute("UPDATE ticket SET urgency = NULL WHERE id = 2")
    assert [j["row_pk"] for j in jobs(conn, "pending")] == [{"id": 2}]
    assert (
        scalar(
            conn, "SELECT count(*) AS n FROM bee.result WHERE is_current AND row_pk = '{\"id\": 2}'"
        )
        == 0
    )


def test_override_policy_until_source_change(
    conn: psycopg.Connection[DictRow], ticket: str
) -> None:
    add_urgency(conn, override_policy="until_source_change")
    for job in claim(conn):
        complete(conn, job, "low")
    conn.execute("UPDATE ticket SET urgency = 'high' WHERE id = 1")
    conn.execute("UPDATE ticket SET body = 'Ora è diverso' WHERE id = 1")
    assert [j["row_pk"] for j in jobs(conn, "pending")] == [{"id": 1}]
    job = claim(conn)[0]
    assert complete(conn, job, "low") == "written"
    assert urgency_of(conn, 1) == "low"


def test_low_confidence_hold_keeps_column_null(
    conn: psycopg.Connection[DictRow], ticket: str
) -> None:
    add_urgency(conn, low_confidence_policy="hold", confidence_threshold=0.8)
    conn.execute("DELETE FROM bee.job WHERE row_pk <> '{\"id\": 1}'::jsonb")
    job = claim(conn)[0]
    assert complete(conn, job, "medium", confidence=0.4) == "held"
    assert urgency_of(conn, 1) is None
    review = conn.execute("SELECT * FROM bee.needs_review").fetchall()
    assert len(review) == 1 and review[0]["value"] == "medium" and review[0]["written"] is False
    assert jobs(conn, "done") != []


def test_low_confidence_write_policy_still_lists_for_review(
    conn: psycopg.Connection[DictRow], ticket: str
) -> None:
    add_urgency(conn)
    job = claim(conn, 1)[0]
    assert complete(conn, job, "medium", confidence=0.2) == "written"
    review = conn.execute("SELECT written FROM bee.needs_review").fetchall()
    assert review == [{"written": True}]


def test_complete_rejects_value_outside_enum(
    conn: psycopg.Connection[DictRow], ticket: str
) -> None:
    add_urgency(conn)
    job = claim(conn, 1)[0]
    with pytest.raises(psycopg.errors.CheckViolation):
        complete(conn, job, "urgentissimo")
    assert jobs(conn, "claimed")[0]["id"] == job["job_id"]


def test_reclaim_stale_returns_abandoned_jobs(
    conn: psycopg.Connection[DictRow], ticket: str
) -> None:
    add_urgency(conn, max_attempts=2)
    claimed = claim(conn)
    assert len(claimed) == 3
    conn.execute(
        "UPDATE bee.job SET claimed_at = now() - interval '10 minutes' WHERE id = %s",
        (claimed[0]["job_id"],),
    )
    conn.execute(
        "UPDATE bee.job SET claimed_at = now() - interval '10 minutes', attempts = 2 WHERE id = %s",
        (claimed[1]["job_id"],),
    )
    n = conn.execute("SELECT bee.reclaim_stale('5 minutes') AS n").fetchone()
    assert n is not None and n["n"] == 2
    by_id = {j["id"]: j for j in jobs(conn)}
    assert by_id[claimed[0]["job_id"]]["status"] == "pending"
    assert by_id[claimed[1]["job_id"]]["status"] == "dead"
    assert by_id[claimed[2]["job_id"]]["status"] == "claimed"


def test_two_workers_claim_disjoint_sets(
    conn: psycopg.Connection[DictRow], conn2: psycopg.Connection[DictRow], ticket: str
) -> None:
    add_urgency(conn)
    a = claim(conn, 2, worker="a")
    b = claim(conn2, 2, worker="b")
    ids_a = {j["job_id"] for j in a}
    ids_b = {j["job_id"] for j in b}
    assert len(ids_a) == 2 and len(ids_b) == 1 and not ids_a & ids_b


def test_claim_skips_disabled_and_filters_by_backend(
    conn: psycopg.Connection[DictRow], ticket: str
) -> None:
    add_urgency(conn)
    conn.execute(
        "SELECT bee.add_column('ticket', 'geo', array['customer'], 'jsonb', p_backend => 'custom', p_model => 'geocoder')"
    )
    only_custom = conn.execute(
        "SELECT backend, model FROM bee.claim_jobs('w', 10, array['custom']::bee.backend[])"
    ).fetchall()
    assert len(only_custom) == 3 and {r["backend"] for r in only_custom} == {"custom"}
    assert {r["model"] for r in only_custom} == {"geocoder"}
    conn.execute("SELECT bee.disable('ticket', 'urgency')")
    assert claim(conn) == []
    assert scalar(conn, "SELECT count(*) AS n FROM bee.job WHERE status = 'pending'") == 0
    conn.execute("INSERT INTO ticket (body) VALUES ('mentre è disabilitata')")
    urgency_def = conn.execute(
        "SELECT id FROM bee.columns WHERE column_name = 'urgency'"
    ).fetchone()
    assert urgency_def is not None
    assert [j for j in jobs(conn, "pending") if j["column_def_id"] == urgency_def["id"]] == []
    n = conn.execute("SELECT bee.enable('ticket', 'urgency') AS n").fetchone()
    assert n is not None and n["n"] == 4


def test_embedding_backend_writes_vector(conn: psycopg.Connection[DictRow], ticket: str) -> None:
    conn.execute(
        "SELECT bee.add_column('ticket', 'embedding', array['body'], 'vector', p_backend => 'embedding',"
        " p_model => 'openai/text-embedding-3-small', p_output_schema => '{\"dimensions\": 3}')"
    )
    col = conn.execute("SELECT bee._column_type('ticket', 'embedding') AS t").fetchone()
    assert col is not None and col["t"] == "vector(3)"
    job = claim(conn, 1)[0]
    assert job["backend"] == "embedding" and job["prompt"] is None
    with pytest.raises(psycopg.errors.CheckViolation):
        complete(conn, job, [0.1, 0.2], confidence=None)
    assert complete(conn, job, [0.1, 0.2, 0.3], confidence=None) == "written"
    vec = conn.execute(
        "SELECT embedding::text AS v FROM ticket WHERE id = %s", (job["row_pk"]["id"],)
    ).fetchone()
    assert vec is not None and vec["v"] == "[0.1,0.2,0.3]"
    with pytest.raises(psycopg.errors.RaiseException, match="dimensions cannot change"):
        conn.execute(
            "SELECT bee.update_column('ticket', 'embedding', p_output_schema => '{\"dimensions\": 4}')"
        )


def test_other_output_types_are_cast(conn: psycopg.Connection[DictRow], ticket: str) -> None:
    for name, otype, _value in [
        ("is_billing", "boolean", True),
        ("sentiment", "numeric", -0.25),
        ("n_items", "integer", 3),
        ("summary", "text", "Riassunto"),
        ("entities", "jsonb", {"customer": "Acme", "products": ["x"]}),
    ]:
        conn.execute(
            "SELECT bee.add_column('ticket', %s, array['body'], %s::bee.output_type, p_prompt => 'p', p_model => 'm')",
            (name, otype),
        )
    conn.execute("DELETE FROM bee.job WHERE row_pk <> '{\"id\": 1}'::jsonb")
    for job in claim(conn):
        value = {
            "is_billing": True,
            "sentiment": -0.25,
            "n_items": 3,
            "summary": "Riassunto",
            "entities": {"customer": "Acme", "products": ["x"]},
        }
        col = conn.execute(
            "SELECT column_name FROM bee.column_def WHERE id = %s", (job["column_def_id"],)
        ).fetchone()
        assert col is not None
        assert complete(conn, job, value[col["column_name"]]) == "written"
    row = conn.execute(
        "SELECT is_billing, sentiment, n_items, summary, entities FROM ticket WHERE id = 1"
    ).fetchone()
    assert row == {
        "is_billing": True,
        "sentiment": pytest.approx(-0.25),
        "n_items": 3,
        "summary": "Riassunto",
        "entities": {"customer": "Acme", "products": ["x"]},
    }


def test_deleted_source_row_drops_the_job(conn: psycopg.Connection[DictRow], ticket: str) -> None:
    add_urgency(conn)
    conn.execute("DELETE FROM ticket WHERE id = 2")
    claimed = claim(conn)
    assert [j["row_pk"] for j in claimed] == [{"id": 1}, {"id": 3}]
    assert len(jobs(conn)) == 2


def test_drop_column_removes_triggers_and_keeps_lineage(
    conn: psycopg.Connection[DictRow], ticket: str
) -> None:
    add_urgency(conn)
    for job in claim(conn):
        complete(conn, job, "low")
    conn.execute("INSERT INTO ticket (body) VALUES ('nuovo')")
    conn.execute("SELECT bee.drop_column('ticket', 'urgency')")
    triggers = conn.execute(
        "SELECT count(*) AS n FROM pg_trigger WHERE tgrelid = 'ticket'::regclass AND NOT tgisinternal"
    ).fetchone()
    assert triggers is not None and triggers["n"] == 0
    assert jobs(conn, "pending") == []
    assert scalar(conn, "SELECT count(*) AS n FROM bee.columns") == 0
    assert scalar(conn, "SELECT count(*) AS n FROM bee.result") == 3
    assert urgency_of(conn, 1) == "low"
    conn.execute("INSERT INTO ticket (body) VALUES ('dopo il drop')")
    assert jobs(conn, "pending") == []
    add_urgency(conn)
    assert len(jobs(conn, "pending")) == 5


def test_composite_and_uuid_primary_keys(conn: psycopg.Connection[DictRow]) -> None:
    conn.execute(
        "CREATE TABLE note (tenant uuid NOT NULL DEFAULT gen_random_uuid(), seq integer NOT NULL,"
        " body text NOT NULL, PRIMARY KEY (tenant, seq))"
    )
    conn.execute("INSERT INTO note (seq, body) VALUES (1, 'prima'), (2, 'seconda')")
    conn.execute(
        "SELECT bee.add_column('note', 'kind', array['body'], 'text', p_prompt => 'p', p_model => 'm')"
    )
    claimed = claim(conn)
    assert len(claimed) == 2
    job = claimed[0]
    assert set(job["row_pk"]) == {"tenant", "seq"}
    assert complete(conn, job, "appunto") == "written"
    row = conn.execute(
        "SELECT kind FROM note WHERE seq = %s AND tenant = %s",
        (job["row_pk"]["seq"], job["row_pk"]["tenant"]),
    ).fetchone()
    assert row is not None and row["kind"] == "appunto"


def test_installer_is_idempotent(database_url: str) -> None:
    from pgbee.installer import install, sql_files

    with psycopg.connect(database_url) as c:
        assert install(c) == []
        versions = c.execute("SELECT version FROM bee.schema_version ORDER BY version").fetchall()
        assert [v[0] for v in versions] == [f.version for f in sql_files()]


def test_decision_backend_definition_rules(conn: psycopg.Connection[DictRow], ticket: str) -> None:
    with pytest.raises(psycopg.errors.RaiseException, match="enum criteria"):
        conn.execute(
            "SELECT bee.add_column('ticket', 'x', array['body'], 'enum', p_backend => 'decision',"
            " p_prompt => 'p', p_model => 'typesafe/jev-1.13', p_output_schema => '[\"a\", \"b\"]')"
        )
    with pytest.raises(psycopg.errors.RaiseException, match="typed questions only"):
        conn.execute(
            "SELECT bee.add_column('ticket', 'x', array['body'], 'text', p_backend => 'decision',"
            " p_prompt => 'p', p_model => 'typesafe/jev-1.13')"
        )
    with pytest.raises(psycopg.errors.RaiseException, match="rubric"):
        conn.execute(
            "SELECT bee.add_column('ticket', 'x', array['body'], 'integer', p_backend => 'decision',"
            " p_prompt => 'p', p_model => 'typesafe/jev-1.13')"
        )
    conn.execute(
        "SELECT bee.add_column('ticket', 'urgency', array['body'], 'enum', p_backend => 'decision',"
        " p_prompt => 'Urgenza', p_model => 'typesafe/jev-1.13',"
        ' p_output_schema => \'{"low": "routine", "high": "shop cannot sell"}\')'
    )
    job = claim(conn, 1)[0]
    assert job["backend"] == "decision"
    with pytest.raises(psycopg.errors.CheckViolation):
        complete(conn, job, "medium")
    row = conn.execute(
        "SELECT bee.complete_job(%s::bigint, %s, '\"high\"'::jsonb, 0.93::real, 'typesafe/jev-1.13', NULL, 90,"
        ' \'{"probabilities": {"high": 0.93, "low": 0.07}}\'::jsonb) AS outcome',
        (job["job_id"], job["source_hash"]),
    ).fetchone()
    assert row is not None and row["outcome"] == "written"
    result = conn.execute(
        "SELECT value, confidence, details FROM bee.result WHERE is_current"
    ).fetchone()
    assert result is not None
    assert result["value"] == "high" and result["details"]["probabilities"]["high"] == 0.93
    assert urgency_of(conn, job["row_pk"]["id"]) == "high"


def test_enum_schema_with_descriptions_works_for_llm_too(
    conn: psycopg.Connection[DictRow], ticket: str
) -> None:
    conn.execute(
        "SELECT bee.add_column('ticket', 'urgency', array['body'], 'enum', p_prompt => 'p', p_model => 'm',"
        ' p_output_schema => \'{"low": "routine", "high": "shop cannot sell"}\')'
    )
    job = claim(conn, 1)[0]
    assert complete(conn, job, "low") == "written"
    with pytest.raises(
        psycopg.errors.RaiseException, match="non-empty array of strings or an object"
    ):
        conn.execute(
            "SELECT bee.add_column('ticket', 'x', array['body'], 'enum', p_prompt => 'p', p_model => 'm',"
            " p_output_schema => '{\"low\": 1}')"
        )


def test_current_version_always_belongs_to_its_column(
    conn: psycopg.Connection[DictRow], ticket: str
) -> None:
    add_urgency(conn)
    conn.execute("SELECT bee.update_column('ticket', 'urgency', p_prompt => 'v2')")
    conn.execute("SELECT bee.drop_column('ticket', 'urgency')")
    add_urgency(conn)
    orphans = scalar(
        conn,
        "SELECT count(*) FROM bee.column_def d LEFT JOIN bee.column_version v"
        " ON v.id = d.current_version_id AND v.column_def_id = d.id WHERE v.id IS NULL",
    )
    assert orphans == 0
