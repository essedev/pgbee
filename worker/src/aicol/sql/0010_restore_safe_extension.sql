-- ai-db 0010: make the extension survive pg_dump and pg_restore.
-- Two things broke a restore of aicol as an extension, where the config tables and their
-- triggers exist before pg_restore loads the data:
-- 1. The foreign key from column_def.current_version_id to column_version.
--    column_version.column_def_id already points the other way, and the cycle breaks pg_restore:
--    whichever table is loaded first violates its key. current_version_id is written only by
--    add_column and update_column, always to a version they have just inserted for the same column.
-- 2. The trigger counting spend on every insert into ai.result: during the restore it recreated
--    the ai.spend rows that the dump also carries. Spend is now counted by complete_job, the only
--    function that stores model results.

ALTER TABLE ai.column_def DROP CONSTRAINT IF EXISTS column_def_current_version_fk;
COMMENT ON COLUMN ai.column_def.current_version_id IS
  'Current ai.column_version of this column. No foreign key (it would form a cycle with column_version.column_def_id and break pg_restore of the extension); only add_column and update_column write it.';

DROP TRIGGER IF EXISTS ai_count_spend ON ai.result;
DROP FUNCTION IF EXISTS ai._count_spend();

CREATE OR REPLACE FUNCTION ai._add_spend(p_def_id bigint, p_usage jsonb) RETURNS void
LANGUAGE sql AS $$
  INSERT INTO ai.spend AS s (column_def_id, day, cost, results)
  VALUES (p_def_id, (now() AT TIME ZONE 'UTC')::date, coalesce((p_usage ->> 'cost')::numeric, 0), 1)
  ON CONFLICT (column_def_id, day)
  DO UPDATE SET cost = s.cost + excluded.cost, results = s.results + 1;
$$;

-- Same body as 0004 plus the spend count. SECURITY DEFINER and search_path repeated (0008):
-- CREATE OR REPLACE resets them.
CREATE OR REPLACE FUNCTION ai.complete_job(
  p_job_id bigint,
  p_source_hash bytea,
  p_value jsonb,
  p_confidence real DEFAULT NULL,
  p_model text DEFAULT NULL,
  p_usage jsonb DEFAULT NULL,
  p_latency_ms integer DEFAULT NULL,
  p_details jsonb DEFAULT NULL
) RETURNS ai.complete_outcome
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
  j ai.job;
  def ai.column_def;
  ver ai.column_version;
  v_value jsonb := p_value;
  v_written boolean := true;
  v_threshold real;
BEGIN
  SELECT * INTO j FROM ai.job WHERE id = p_job_id FOR UPDATE;
  IF j.id IS NULL OR j.status <> 'claimed' THEN
    RETURN 'cancelled';
  END IF;
  SELECT * INTO def FROM ai.column_def WHERE id = j.column_def_id;
  SELECT * INTO ver FROM ai.column_version WHERE id = def.current_version_id;
  IF v_value IS NOT NULL AND jsonb_typeof(v_value) = 'null' THEN
    v_value := NULL;
  END IF;
  PERFORM ai._validate_value(def.output_type, ver.output_schema, v_value);

  IF j.source_hash <> p_source_hash THEN
    INSERT INTO ai.result (column_def_id, column_version_id, row_pk, source_hash, value, confidence, source,
                           is_current, written, model, usage, latency_ms, details)
    VALUES (def.id, ver.id, j.row_pk, p_source_hash, v_value, p_confidence, 'model',
            false, false, coalesce(p_model, ver.model), p_usage, p_latency_ms, p_details);
    PERFORM ai._add_spend(def.id, p_usage);
    UPDATE ai.job SET status = 'pending', claimed_by = NULL, claimed_at = NULL,
                      next_attempt_at = now(), updated_at = now()
    WHERE id = j.id;
    RETURN 'stale_requeued';
  END IF;

  IF def.deleted_at IS NOT NULL THEN
    UPDATE ai.job SET status = 'done', updated_at = now() WHERE id = j.id;
    RETURN 'cancelled';
  END IF;

  v_threshold := (def.config ->> 'confidence_threshold')::real;
  IF ver.backend IN ('llm', 'decision') AND p_confidence IS NOT NULL AND v_threshold IS NOT NULL
     AND p_confidence < v_threshold AND def.config ->> 'low_confidence_policy' = 'hold' THEN
    v_written := false;
  END IF;

  IF v_written THEN
    PERFORM set_config('ai.writer', 'worker', true);
    EXECUTE format('UPDATE %I.%I t SET %I = %s WHERE %s',
                   def.table_schema, def.table_name, def.column_name,
                   ai._cast_expr(def.output_type, '$2'), ai._pk_where(def))
    USING j.row_pk, v_value;
    PERFORM set_config('ai.writer', '', true);
  END IF;

  UPDATE ai.result SET is_current = false
  WHERE column_def_id = def.id AND row_pk = j.row_pk AND is_current;
  INSERT INTO ai.result (column_def_id, column_version_id, row_pk, source_hash, value, confidence, source,
                         is_current, written, model, usage, latency_ms, details)
  VALUES (def.id, ver.id, j.row_pk, p_source_hash, v_value, p_confidence, 'model',
          true, v_written, coalesce(p_model, ver.model), p_usage, p_latency_ms, p_details);
  PERFORM ai._add_spend(def.id, p_usage);
  UPDATE ai.job SET status = 'done', last_error = NULL, updated_at = now() WHERE id = j.id;
  RETURN CASE WHEN v_written THEN 'written'::ai.complete_outcome ELSE 'held'::ai.complete_outcome END;
END $$;
