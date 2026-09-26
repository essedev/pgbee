-- ai-db 0007: incremental backfill.
-- ai.backfill used to scan the whole table in one statement inside add_column, update_column or
-- enable: on 1M rows that is 39 s with the user table write-locked (ALTER TABLE holds the lock
-- until commit) and a million pending jobs. Now it enqueues one chunk in primary key order and
-- keeps a cursor; claim_jobs enqueues the next chunks when the queue of that column runs low.

ALTER TABLE ai.column_def
  ADD COLUMN IF NOT EXISTS backfill_pending boolean NOT NULL DEFAULT false,
  ADD COLUMN IF NOT EXISTS backfill_cursor jsonb,
  ADD COLUMN IF NOT EXISTS backfill_scanned bigint NOT NULL DEFAULT 0;
COMMENT ON COLUMN ai.column_def.backfill_cursor IS
  'Primary key of the last row scanned by the running backfill; NULL before the first chunk.';

CREATE OR REPLACE FUNCTION ai._default_config() RETURNS jsonb
LANGUAGE sql IMMUTABLE AS $$
  SELECT '{
    "batch_size": 20,
    "max_attempts": 5,
    "backoff_base_seconds": 30,
    "confidence_threshold": 0.7,
    "low_confidence_policy": "write",
    "override_policy": "pin",
    "concurrency": 4,
    "budget_usd": null,
    "budget_period": "month",
    "backfill_chunk": 1000
  }'::jsonb;
$$;

CREATE OR REPLACE FUNCTION ai._backfill_chunk(p_config jsonb) RETURNS integer
LANGUAGE sql IMMUTABLE AS $$
  SELECT coalesce((p_config ->> 'backfill_chunk')::integer, 1000);
$$;

CREATE OR REPLACE FUNCTION ai._validate_config() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  budget jsonb := NEW.config -> 'budget_usd';
  chunk jsonb := NEW.config -> 'backfill_chunk';
BEGIN
  IF budget IS NOT NULL AND jsonb_typeof(budget) <> 'null'
     AND (jsonb_typeof(budget) <> 'number' OR (budget #>> '{}')::numeric < 0) THEN
    RAISE EXCEPTION 'ai: budget_usd must be a number >= 0 (USD) or null for no cap, got %', budget;
  END IF;
  IF NEW.config ? 'budget_period'
     AND NOT coalesce(NEW.config ->> 'budget_period' = ANY (ARRAY['day', 'month', 'total']), false) THEN
    RAISE EXCEPTION 'ai: budget_period must be day, month or total, got %', NEW.config -> 'budget_period';
  END IF;
  IF chunk IS NOT NULL AND (jsonb_typeof(chunk) <> 'number'
     OR (chunk #>> '{}')::numeric <> trunc((chunk #>> '{}')::numeric)
     OR (chunk #>> '{}')::numeric NOT BETWEEN 1 AND 100000) THEN
    RAISE EXCEPTION 'ai: backfill_chunk must be an integer between 1 and 100000, got %', chunk;
  END IF;
  RETURN NEW;
END $$;

-- One chunk: the next p_chunk rows after the cursor in primary key order. Enqueues those whose
-- current value is missing, stale or computed on other sources, moves the cursor, and closes
-- the backfill when the table is exhausted. Returns the number of jobs enqueued.
CREATE OR REPLACE FUNCTION ai._backfill_step(p_def_id bigint, p_chunk integer) RETURNS bigint
LANGUAGE plpgsql AS $$
DECLARE
  def ai.column_def;
  v_rel regclass;
  v_cols text;
  v_cols_desc text;
  v_cursor text;
  v_cond text;
  v_scanned bigint;
  v_enqueued bigint;
  v_last jsonb;
BEGIN
  SELECT * INTO def FROM ai.column_def WHERE id = p_def_id;
  IF def.id IS NULL OR def.deleted_at IS NOT NULL OR NOT def.backfill_pending THEN
    RETURN 0;
  END IF;
  v_rel := format('%I.%I', def.table_schema, def.table_name)::regclass;
  SELECT string_agg(format('t.%I', c), ', ' ORDER BY o),
         string_agg(format('t.%I DESC', c), ', ' ORDER BY o),
         string_agg(format('($5 ->> %L)::%s', c, ai._column_type(v_rel, c)), ', ' ORDER BY o)
  INTO v_cols, v_cols_desc, v_cursor
  FROM unnest(def.pk_columns) WITH ORDINALITY AS u (c, o);
  v_cond := CASE WHEN def.backfill_cursor IS NULL THEN 'true'
                 ELSE format('(%s) > (%s)', v_cols, v_cursor) END;
  EXECUTE format($q$
    WITH chunk AS (
      SELECT t.* FROM %I.%I t WHERE %s ORDER BY %s LIMIT $6
    ), rows AS (
      SELECT ai._row_pk(to_jsonb(t), $1) AS pk, ai._source_hash(to_jsonb(t), $2) AS h
      FROM chunk t
    ), todo AS (
      SELECT r.pk, r.h FROM rows r
      WHERE NOT EXISTS (
        SELECT 1 FROM ai.result x
        WHERE x.column_def_id = $3 AND x.row_pk = r.pk AND x.is_current
          AND (x.source = 'human' OR (x.source_hash = r.h AND x.column_version_id = $4)))
    ), ins AS (
      INSERT INTO ai.job (column_def_id, row_pk, source_hash)
      SELECT $3, pk, h FROM todo
      ON CONFLICT (column_def_id, row_pk) WHERE status IN ('pending', 'claimed')
      DO UPDATE SET source_hash = EXCLUDED.source_hash, updated_at = now()
      RETURNING 1
    )
    SELECT (SELECT count(*) FROM chunk), (SELECT count(*) FROM ins),
           (SELECT ai._row_pk(to_jsonb(t), $1) FROM chunk t ORDER BY %s LIMIT 1)
  $q$, def.table_schema, def.table_name, v_cond, v_cols, v_cols_desc)
  INTO v_scanned, v_enqueued, v_last
  USING def.pk_columns, def.source_columns, def.id, def.current_version_id, def.backfill_cursor, p_chunk;

  UPDATE ai.column_def
  SET backfill_pending = v_scanned >= p_chunk,
      backfill_cursor = CASE WHEN v_scanned >= p_chunk THEN v_last END,
      backfill_scanned = backfill_scanned + v_scanned
  WHERE id = def.id;
  IF v_enqueued > 0 THEN
    PERFORM pg_notify('ai_jobs', def.id::text);
  END IF;
  RETURN v_enqueued;
END $$;

CREATE OR REPLACE FUNCTION ai.backfill(p_def_id bigint) RETURNS bigint
LANGUAGE plpgsql AS $$
DECLARE
  def ai.column_def;
BEGIN
  SELECT * INTO def FROM ai.column_def WHERE id = p_def_id AND deleted_at IS NULL;
  IF def.id IS NULL THEN
    RAISE EXCEPTION 'ai: derived column % does not exist', p_def_id;
  END IF;
  UPDATE ai.column_def
  SET backfill_pending = true, backfill_cursor = NULL, backfill_scanned = 0
  WHERE id = def.id;
  RETURN ai._backfill_step(def.id, ai._backfill_chunk(def.config));
END $$;
COMMENT ON FUNCTION ai.backfill(bigint) IS
  'Start a scan of the table that enqueues every row whose current value is missing, stale or computed on different sources, skipping human-pinned rows. Enqueues the first chunk (config backfill_chunk, default 1000 rows in primary key order) and returns how many jobs it added; claim_jobs enqueues the next chunks as the queue drains.';

-- Called by claim_jobs before and after it claims: for each column with a running backfill, enqueue chunks until its queue
-- holds a chunk worth of pending jobs (at most ten chunks per call). A column locked by another
-- claimer is skipped. When chunks were scanned and the scan is not over, a NOTIFY makes the
-- workers claim again right away instead of waiting for their poll.
CREATE OR REPLACE FUNCTION ai._drive_backfills() RETURNS void
LANGUAGE plpgsql AS $$
DECLARE
  d record;
  v_chunk integer;
  v_queued bigint;
  v_steps integer;
  v_still boolean;
BEGIN
  FOR d IN
    SELECT id, config FROM ai.column_def
    WHERE backfill_pending AND enabled AND deleted_at IS NULL
    FOR NO KEY UPDATE SKIP LOCKED
  LOOP
    CONTINUE WHEN ai._over_budget(d.id, d.config);
    v_chunk := ai._backfill_chunk(d.config);
    SELECT count(*) INTO v_queued
    FROM (SELECT 1 FROM ai.job WHERE column_def_id = d.id AND status = 'pending' LIMIT v_chunk) q;
    v_steps := 0;
    v_still := true;
    WHILE v_queued < v_chunk AND v_steps < 10 AND v_still LOOP
      v_queued := v_queued + ai._backfill_step(d.id, v_chunk);
      v_steps := v_steps + 1;
      SELECT backfill_pending INTO v_still FROM ai.column_def WHERE id = d.id;
    END LOOP;
    IF v_steps > 0 AND v_still THEN
      PERFORM pg_notify('ai_jobs', d.id::text);
    END IF;
  END LOOP;
END $$;

CREATE OR REPLACE FUNCTION ai.claim_jobs(p_worker_id text, p_batch_size integer DEFAULT 20, p_backends ai.backend[] DEFAULT NULL)
RETURNS TABLE (
  job_id bigint,
  column_def_id bigint,
  column_version_id bigint,
  backend ai.backend,
  model text,
  prompt text,
  output_type ai.output_type,
  output_schema jsonb,
  backend_config jsonb,
  config jsonb,
  row_pk jsonb,
  source_hash bytea,
  source jsonb,
  attempts integer
)
LANGUAGE plpgsql AS $$
DECLARE
  j record;
  def ai.column_def;
  src jsonb;
BEGIN
  PERFORM ai._drive_backfills();
  FOR j IN
    WITH open_defs AS (
      -- Budget checked once per definition, not per job.
      SELECT d.id
      FROM ai.column_def d
      JOIN ai.column_version v ON v.id = d.current_version_id
      WHERE d.enabled AND d.deleted_at IS NULL
        AND (p_backends IS NULL OR v.backend = ANY (p_backends))
        AND NOT ai._over_budget(d.id, d.config)
    ), picked AS (
      SELECT jb.id
      FROM ai.job jb
      JOIN open_defs od ON od.id = jb.column_def_id
      WHERE jb.status = 'pending' AND jb.next_attempt_at <= now()
      ORDER BY jb.next_attempt_at, jb.id
      LIMIT p_batch_size
      FOR UPDATE OF jb SKIP LOCKED
    ), picked_decisions AS (
      SELECT d.table_schema, d.table_name, pj.row_pk, v.model
      FROM picked p
      JOIN ai.job pj ON pj.id = p.id
      JOIN ai.column_def d ON d.id = pj.column_def_id
      JOIN ai.column_version v ON v.id = d.current_version_id
      WHERE v.backend = 'decision'
    ), siblings AS (
      -- Ready decision jobs of other columns on the same rows and model come along, so the
      -- worker can ask all the questions on a row in one call even during a backfill.
      SELECT jb.id
      FROM ai.job jb
      JOIN open_defs od ON od.id = jb.column_def_id
      JOIN ai.column_def d ON d.id = jb.column_def_id
      JOIN ai.column_version v ON v.id = d.current_version_id
      JOIN picked_decisions pd ON pd.table_schema = d.table_schema AND pd.table_name = d.table_name
                              AND pd.row_pk = jb.row_pk AND pd.model = v.model
      WHERE jb.status = 'pending' AND jb.next_attempt_at <= now() AND v.backend = 'decision'
        AND jb.id NOT IN (SELECT id FROM picked)
      FOR UPDATE OF jb SKIP LOCKED
    ), claimed AS (
      UPDATE ai.job jb
      SET status = 'claimed', claimed_by = p_worker_id, claimed_at = now(),
          attempts = jb.attempts + 1, updated_at = now()
      FROM (SELECT id FROM picked UNION SELECT id FROM siblings) picked
      WHERE jb.id = picked.id
      RETURNING jb.id, jb.column_def_id, jb.row_pk, jb.source_hash, jb.attempts
    )
    SELECT c.id, c.column_def_id, c.row_pk, c.source_hash, c.attempts,
           v.id AS version_id, v.backend, v.model, v.prompt, v.output_schema, v.backend_config,
           d.output_type, d.config
    FROM claimed c
    JOIN ai.column_def d ON d.id = c.column_def_id
    JOIN ai.column_version v ON v.id = d.current_version_id
  LOOP
    SELECT * INTO def FROM ai.column_def WHERE id = j.column_def_id;
    EXECUTE format(
      'SELECT (SELECT jsonb_object_agg(k, to_jsonb(t) -> k) FROM unnest($2) AS k) FROM %I.%I t WHERE %s',
      def.table_schema, def.table_name, ai._pk_where(def))
    INTO src USING j.row_pk, def.source_columns;
    IF src IS NULL THEN
      -- The source row is gone: nothing left to compute.
      DELETE FROM ai.job WHERE id = j.id;
      CONTINUE;
    END IF;
    job_id := j.id;
    column_def_id := j.column_def_id;
    column_version_id := j.version_id;
    backend := j.backend;
    model := j.model;
    prompt := j.prompt;
    output_type := j.output_type;
    output_schema := j.output_schema;
    backend_config := j.backend_config;
    config := j.config;
    row_pk := j.row_pk;
    source_hash := j.source_hash;
    source := src;
    attempts := j.attempts;
    RETURN NEXT;
  END LOOP;
  -- Refill after taking: the next claim finds the queue ready and the NOTIFY wakes a worker.
  PERFORM ai._drive_backfills();
END $$;
COMMENT ON FUNCTION ai.claim_jobs(text, integer, ai.backend[]) IS
  'Worker contract. Lock up to batch_size ready jobs (SKIP LOCKED), mark them claimed and return each with its source values and current version. Optionally restricted to some backends. Columns over budget are skipped. Decision jobs bring along the ready decision jobs of the same rows and model, so a batch can exceed batch_size. Advances the backfills whose queue runs low.';


CREATE OR REPLACE VIEW ai.columns AS
SELECT d.id, d.table_schema, d.table_name, d.column_name, d.source_columns, d.output_type, d.enabled,
       v.version, v.backend, v.model, v.prompt, v.output_schema, v.backend_config, d.config,
       (SELECT count(*) FROM ai.job j WHERE j.column_def_id = d.id AND j.status = 'pending') AS pending,
       (SELECT count(*) FROM ai.job j WHERE j.column_def_id = d.id AND j.status = 'claimed') AS claimed,
       (SELECT count(*) FROM ai.job j WHERE j.column_def_id = d.id AND j.status = 'done') AS done,
       (SELECT count(*) FROM ai.job j WHERE j.column_def_id = d.id AND j.status = 'dead') AS dead,
       (SELECT count(*) FROM ai.result r WHERE r.column_def_id = d.id AND r.is_current AND r.source = 'human') AS human_overrides,
       (SELECT count(*) FROM ai.result r WHERE r.column_def_id = d.id AND r.is_current AND r.source = 'model'
          AND r.column_version_id <> d.current_version_id) AS stale,
       d.backfill_pending, d.backfill_scanned
FROM ai.column_def d
JOIN ai.column_version v ON v.id = d.current_version_id
WHERE d.deleted_at IS NULL;

COMMENT ON FUNCTION ai.enable(regclass, text) IS
  'Resume a derived column and start a backfill for the rows changed while it was disabled. Returns the jobs enqueued by the first chunk; claim_jobs enqueues the rest.';
