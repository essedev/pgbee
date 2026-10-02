-- pgbee 0017: queue management for one row of a derived column.

CREATE FUNCTION bee.requeue(p_table regclass, p_column text, p_row_pk jsonb) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
  def bee.column_def;
  rec jsonb;
  pk jsonb;
BEGIN
  def := bee._find_def(p_table, p_column);
  IF NOT def.enabled THEN
    RETURN false;
  END IF;
  -- Lock the source row before touching its job, in the same order as the enqueue trigger.
  EXECUTE format('SELECT to_jsonb(t) FROM %I.%I t WHERE %s FOR NO KEY UPDATE',
                 def.table_schema, def.table_name, bee._pk_where(def))
  INTO rec USING p_row_pk;
  IF rec IS NULL THEN
    RETURN false;
  END IF;
  pk := bee._row_pk(rec, def.pk_columns);
  IF bee._is_pinned(def.id, pk) THEN
    RETURN false;
  END IF;
  PERFORM bee._enqueue(def.id, pk, bee._source_hash(rec, def.source_columns));
  RETURN true;
END $$;
COMMENT ON FUNCTION bee.requeue(regclass, text, jsonb) IS
  'Force a job for one existing row using its current source hash, even when a current model result matches. Refreshes an existing live job. Returns false for a missing row, disabled column or current human result; never removes a human override.';

CREATE FUNCTION bee.retry_dead_row(p_table regclass, p_column text, p_row_pk jsonb) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
  def bee.column_def;
  v_job_id bigint;
BEGIN
  def := bee._find_def(p_table, p_column);
  -- A live job already covers this row. Match retry_dead by dropping obsolete dead jobs.
  DELETE FROM bee.job d
  WHERE d.column_def_id = def.id AND d.row_pk = p_row_pk AND d.status = 'dead'
    AND EXISTS (SELECT 1 FROM bee.job l WHERE l.column_def_id = d.column_def_id
                AND l.row_pk = d.row_pk AND l.status IN ('pending', 'claimed'));
  SELECT id INTO v_job_id FROM bee.job
  WHERE column_def_id = def.id AND row_pk = p_row_pk AND status = 'dead'
  ORDER BY id DESC LIMIT 1 FOR UPDATE;
  IF v_job_id IS NULL THEN
    RETURN false;
  END IF;
  -- More than one dead attempt can exist after an explicit requeue. Retry only the newest.
  DELETE FROM bee.job
  WHERE column_def_id = def.id AND row_pk = p_row_pk AND status = 'dead' AND id <> v_job_id;
  UPDATE bee.job SET status = 'pending', attempts = 0, next_attempt_at = now(),
                     updated_at = now()
  WHERE id = v_job_id AND status = 'dead';
  IF NOT FOUND THEN
    RETURN false;
  END IF;
  PERFORM pg_notify('bee_jobs', def.id::text);
  RETURN true;
END $$;
COMMENT ON FUNCTION bee.retry_dead_row(regclass, text, jsonb) IS
  'Retry only the newest dead job for one row with a fresh attempt budget and immediate scheduling, retaining its source hash and last error. Drops older dead jobs for that row, or all dead jobs when a live job exists. Returns false when none is retried.';

REVOKE EXECUTE ON FUNCTION bee.requeue(regclass, text, jsonb),
  bee.retry_dead_row(regclass, text, jsonb) FROM PUBLIC;
-- Management calls remain with the extension owner, who may grant EXECUTE to an application role.
-- bee_worker only receives the worker contract in 0008 and must not schedule paid work.
