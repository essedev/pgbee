-- pgbee 0014: complete_job takes locks in the application's order.
-- An UPDATE of a source column locks the row, then the enqueue trigger locks the live job.
-- complete_job locked the job first and the row after, to write the value: when a row was
-- updated while its value was being completed, Postgres killed one of the two transactions as
-- a deadlock, sometimes the application's (found by bench/failure, reproduced in
-- test_row_update_while_the_worker_completes_does_not_deadlock). Now the row is locked first.
-- No signature changes.

-- Same body as 0011 but the locking at the start. SECURITY DEFINER and search_path repeated
-- (0008): CREATE OR REPLACE resets them.
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
  -- Lock order of the application: an UPDATE locks the row, then its enqueue trigger locks the
  -- job. Locking the job first deadlocked with it, so the job is read without a lock to learn
  -- the row, the row is locked, and only then the job, checked again.
  SELECT * INTO j FROM bee.job WHERE id = p_job_id;
  IF j.id IS NULL OR j.status <> 'claimed' THEN
    RETURN 'cancelled';
  END IF;
  SELECT * INTO def FROM bee.column_def WHERE id = j.column_def_id;
  EXECUTE format('SELECT 1 FROM %I.%I t WHERE %s FOR NO KEY UPDATE',
                 def.table_schema, def.table_name, bee._pk_where(def))
  USING j.row_pk;
  SELECT * INTO j FROM bee.job WHERE id = p_job_id FOR UPDATE;
  IF j.id IS NULL OR j.status <> 'claimed' THEN
    RETURN 'cancelled';
  END IF;
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
