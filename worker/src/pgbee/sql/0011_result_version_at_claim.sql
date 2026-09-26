-- pgbee 0011: a result is recorded under the version the worker computed it with.
-- complete_job used to attach every result to the version current at completion time. If
-- update_column ran while a job was being computed, the value produced with the old prompt was
-- recorded as the new version, counted as up to date, and never recomputed (found in review,
-- reproduced on the database). claim_jobs now stores the version in the job, and complete_job
-- treats a version change like a source change: the value goes to the lineage under its real
-- version, not current, and the row is queued again. No signature changes.

ALTER TABLE bee.job ADD COLUMN IF NOT EXISTS claimed_version_id bigint;
COMMENT ON COLUMN bee.job.claimed_version_id IS
  'Version current when the job was claimed: the prompt and model the worker computes with.';

-- SECURITY DEFINER and search_path repeated (0008): CREATE OR REPLACE resets them.
CREATE OR REPLACE FUNCTION bee.claim_jobs(p_worker_id text, p_batch_size integer DEFAULT 20, p_backends bee.backend[] DEFAULT NULL)
RETURNS TABLE (
  job_id bigint,
  column_def_id bigint,
  column_version_id bigint,
  backend bee.backend,
  model text,
  prompt text,
  output_type bee.output_type,
  output_schema jsonb,
  backend_config jsonb,
  config jsonb,
  row_pk jsonb,
  source_hash bytea,
  source jsonb,
  attempts integer
)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
  j record;
  def bee.column_def;
  src jsonb;
BEGIN
  PERFORM bee._drive_backfills();
  FOR j IN
    WITH open_defs AS (
      -- Budget checked once per definition, not per job.
      SELECT d.id
      FROM bee.column_def d
      JOIN bee.column_version v ON v.id = d.current_version_id
      WHERE d.enabled AND d.deleted_at IS NULL
        AND (p_backends IS NULL OR v.backend = ANY (p_backends))
        AND NOT bee._over_budget(d.id, d.config)
    ), picked AS (
      SELECT jb.id
      FROM bee.job jb
      JOIN open_defs od ON od.id = jb.column_def_id
      WHERE jb.status = 'pending' AND jb.next_attempt_at <= now()
      ORDER BY jb.next_attempt_at, jb.id
      LIMIT p_batch_size
      FOR UPDATE OF jb SKIP LOCKED
    ), picked_decisions AS (
      SELECT d.table_schema, d.table_name, pj.row_pk, v.model
      FROM picked p
      JOIN bee.job pj ON pj.id = p.id
      JOIN bee.column_def d ON d.id = pj.column_def_id
      JOIN bee.column_version v ON v.id = d.current_version_id
      WHERE v.backend = 'decision'
    ), siblings AS (
      -- Ready decision jobs of other columns on the same rows and model come along, so the
      -- worker can ask all the questions on a row in one call even during a backfill.
      SELECT jb.id
      FROM bee.job jb
      JOIN open_defs od ON od.id = jb.column_def_id
      JOIN bee.column_def d ON d.id = jb.column_def_id
      JOIN bee.column_version v ON v.id = d.current_version_id
      JOIN picked_decisions pd ON pd.table_schema = d.table_schema AND pd.table_name = d.table_name
                              AND pd.row_pk = jb.row_pk AND pd.model = v.model
      WHERE jb.status = 'pending' AND jb.next_attempt_at <= now() AND v.backend = 'decision'
        AND jb.id NOT IN (SELECT id FROM picked)
      FOR UPDATE OF jb SKIP LOCKED
    ), claimed AS (
      UPDATE bee.job jb
      SET status = 'claimed', claimed_by = p_worker_id, claimed_at = now(),
          attempts = jb.attempts + 1, updated_at = now(),
          claimed_version_id = d.current_version_id
      FROM (SELECT id FROM picked UNION SELECT id FROM siblings) picked, bee.column_def d
      WHERE jb.id = picked.id AND d.id = jb.column_def_id
      RETURNING jb.id, jb.column_def_id, jb.row_pk, jb.source_hash, jb.attempts
    )
    SELECT c.id, c.column_def_id, c.row_pk, c.source_hash, c.attempts,
           v.id AS version_id, v.backend, v.model, v.prompt, v.output_schema, v.backend_config,
           d.output_type, d.config
    FROM claimed c
    JOIN bee.column_def d ON d.id = c.column_def_id
    JOIN bee.column_version v ON v.id = d.current_version_id
  LOOP
    SELECT * INTO def FROM bee.column_def WHERE id = j.column_def_id;
    EXECUTE format(
      'SELECT (SELECT jsonb_object_agg(k, to_jsonb(t) -> k) FROM unnest($2) AS k) FROM %I.%I t WHERE %s',
      def.table_schema, def.table_name, bee._pk_where(def))
    INTO src USING j.row_pk, def.source_columns;
    IF src IS NULL THEN
      -- The source row is gone: nothing left to compute.
      DELETE FROM bee.job WHERE id = j.id;
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
  PERFORM bee._drive_backfills();
END $$;
COMMENT ON FUNCTION bee.claim_jobs(text, integer, bee.backend[]) IS
  'Worker contract. Lock up to batch_size ready jobs (SKIP LOCKED), mark them claimed and return each with its source values and current version. Optionally restricted to some backends. Columns over budget are skipped. Decision jobs bring along the ready decision jobs of the same rows and model, so a batch can exceed batch_size. Advances the backfills whose queue runs low.';



CREATE OR REPLACE FUNCTION bee.complete_job(
  p_job_id bigint,
  p_source_hash bytea,
  p_value jsonb,
  p_confidence real DEFAULT NULL,
  p_model text DEFAULT NULL,
  p_usage jsonb DEFAULT NULL,
  p_latency_ms integer DEFAULT NULL,
  p_details jsonb DEFAULT NULL
) RETURNS bee.complete_outcome
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
  j bee.job;
  def bee.column_def;
  ver bee.column_version;
  used bee.column_version;
  v_value jsonb := p_value;
  v_written boolean := true;
  v_threshold real;
BEGIN
  SELECT * INTO j FROM bee.job WHERE id = p_job_id FOR UPDATE;
  IF j.id IS NULL OR j.status <> 'claimed' THEN
    RETURN 'cancelled';
  END IF;
  SELECT * INTO def FROM bee.column_def WHERE id = j.column_def_id;
  SELECT * INTO ver FROM bee.column_version WHERE id = def.current_version_id;
  -- The version the worker computed with: the one current at claim time. Jobs claimed before
  -- 0011 have no claimed_version_id and fall back to the current one.
  SELECT * INTO used FROM bee.column_version WHERE id = coalesce(j.claimed_version_id, ver.id);
  IF v_value IS NOT NULL AND jsonb_typeof(v_value) = 'null' THEN
    v_value := NULL;
  END IF;
  PERFORM bee._validate_value(def.output_type, used.output_schema, v_value);

  -- The row changed, or a new version arrived, while the worker was computing: keep the value in
  -- the lineage under the version it was computed with, not current, and queue the row again.
  IF j.source_hash <> p_source_hash OR used.id <> ver.id THEN
    INSERT INTO bee.result (column_def_id, column_version_id, row_pk, source_hash, value, confidence, source,
                           is_current, written, model, usage, latency_ms, details)
    VALUES (def.id, used.id, j.row_pk, p_source_hash, v_value, p_confidence, 'model',
            false, false, coalesce(p_model, used.model), p_usage, p_latency_ms, p_details);
    PERFORM bee._add_spend(def.id, p_usage);
    UPDATE bee.job SET status = 'pending', claimed_by = NULL, claimed_at = NULL,
                      claimed_version_id = NULL, next_attempt_at = now(), updated_at = now()
    WHERE id = j.id;
    RETURN 'stale_requeued';
  END IF;

  IF def.deleted_at IS NOT NULL THEN
    UPDATE bee.job SET status = 'done', updated_at = now() WHERE id = j.id;
    RETURN 'cancelled';
  END IF;

  v_threshold := (def.config ->> 'confidence_threshold')::real;
  IF ver.backend IN ('llm', 'decision') AND p_confidence IS NOT NULL AND v_threshold IS NOT NULL
     AND p_confidence < v_threshold AND def.config ->> 'low_confidence_policy' = 'hold' THEN
    v_written := false;
  END IF;

  IF v_written THEN
    PERFORM set_config('bee.writer', 'worker', true);
    EXECUTE format('UPDATE %I.%I t SET %I = %s WHERE %s',
                   def.table_schema, def.table_name, def.column_name,
                   bee._cast_expr(def.output_type, '$2'), bee._pk_where(def))
    USING j.row_pk, v_value;
    PERFORM set_config('bee.writer', '', true);
  END IF;

  UPDATE bee.result SET is_current = false
  WHERE column_def_id = def.id AND row_pk = j.row_pk AND is_current;
  INSERT INTO bee.result (column_def_id, column_version_id, row_pk, source_hash, value, confidence, source,
                         is_current, written, model, usage, latency_ms, details)
  VALUES (def.id, ver.id, j.row_pk, p_source_hash, v_value, p_confidence, 'model',
          true, v_written, coalesce(p_model, ver.model), p_usage, p_latency_ms, p_details);
  PERFORM bee._add_spend(def.id, p_usage);
  UPDATE bee.job SET status = 'done', last_error = NULL, updated_at = now() WHERE id = j.id;
  RETURN CASE WHEN v_written THEN 'written'::bee.complete_outcome ELSE 'held'::bee.complete_outcome END;
END $$;
