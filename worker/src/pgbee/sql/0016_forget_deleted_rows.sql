-- pgbee 0016: deleted rows leave the lineage, vectors stay out of it, bee.definition.
--
-- A deleted row kept its current result forever: bee.prune only removes superseded results, so an
-- application that replaces rows (chunks rewritten at every ingestion) accumulated orphans; and a
-- row deleted and inserted again with the same key and sources was never computed, because the
-- enqueue trigger found a current result with the same hash. Two triggers per derived column now
-- retire the current result and drop the live jobs of a deleted row (bee_forget_<column>) or of a
-- truncated table (bee_forget_all_<column>). Existing orphans are retired below, once.
--
-- The lineage copied every value, vectors included: a 1024-dimension embedding is about 20 kB of
-- jsonb against 2 kB in a halfvec column. Results of vector and halfvec columns now keep no value:
-- the column holds the current one, and a vector is not something to read back from the lineage.

CREATE OR REPLACE FUNCTION bee.forget_trigger() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
  def bee.column_def;
  pk jsonb;
BEGIN
  SELECT * INTO def FROM bee.column_def WHERE id = TG_ARGV[0]::bigint;
  IF def.id IS NULL OR def.deleted_at IS NOT NULL THEN
    RETURN NULL;
  END IF;
  IF TG_OP = 'TRUNCATE' THEN
    UPDATE bee.result SET is_current = false WHERE column_def_id = def.id AND is_current;
    DELETE FROM bee.job WHERE column_def_id = def.id AND status IN ('pending', 'claimed');
    RETURN NULL;
  END IF;
  pk := bee._row_pk(to_jsonb(OLD), def.pk_columns);
  UPDATE bee.result SET is_current = false
  WHERE column_def_id = def.id AND row_pk = pk AND is_current;
  DELETE FROM bee.job
  WHERE column_def_id = def.id AND row_pk = pk AND status IN ('pending', 'claimed');
  RETURN NULL;
END $$;

CREATE OR REPLACE FUNCTION bee._install_forget_triggers(p_def bee.column_def) RETURNS void
LANGUAGE plpgsql AS $$
BEGIN
  EXECUTE format(
    'CREATE OR REPLACE TRIGGER %I AFTER DELETE ON %I.%I FOR EACH ROW EXECUTE FUNCTION bee.forget_trigger(%L)',
    'bee_forget_' || p_def.column_name, p_def.table_schema, p_def.table_name, p_def.id);
  EXECUTE format(
    'CREATE OR REPLACE TRIGGER %I AFTER TRUNCATE ON %I.%I FOR EACH STATEMENT EXECUTE FUNCTION bee.forget_trigger(%L)',
    'bee_forget_all_' || p_def.column_name, p_def.table_schema, p_def.table_name, p_def.id);
END $$;

-- Retire the current results of rows that no longer exist. One anti-join over the table.
CREATE OR REPLACE FUNCTION bee._forget_missing_rows(p_def bee.column_def) RETURNS bigint
LANGUAGE plpgsql AS $$
DECLARE
  n bigint;
BEGIN
  EXECUTE format($q$
    WITH gone AS (
      UPDATE bee.result r SET is_current = false
      WHERE r.column_def_id = $1 AND r.is_current
        AND NOT EXISTS (SELECT 1 FROM %I.%I t WHERE bee._row_pk(to_jsonb(t), $2) = r.row_pk)
      RETURNING 1
    )
    SELECT count(*) FROM gone
  $q$, p_def.table_schema, p_def.table_name)
  INTO n
  USING p_def.id, p_def.pk_columns;
  RETURN n;
END $$;

-- Existing derived columns: triggers, orphans retired, vectors dropped from the lineage.
DO $$
DECLARE
  def bee.column_def;
BEGIN
  FOR def IN
    SELECT d.* FROM bee.column_def d
    WHERE d.deleted_at IS NULL AND to_regclass(format('%I.%I', d.table_schema, d.table_name)) IS NOT NULL
  LOOP
    PERFORM bee._install_forget_triggers(def);
    PERFORM bee._forget_missing_rows(def);
    IF def.output_type::text IN ('vector', 'halfvec') THEN
      UPDATE bee.result SET value = NULL WHERE column_def_id = def.id AND value IS NOT NULL;
    END IF;
  END LOOP;
END $$;

-- Same body as 0001 plus the forget triggers.
CREATE OR REPLACE FUNCTION bee.add_column(
  p_table regclass,
  p_column text,
  p_source_columns text[],
  p_output_type bee.output_type,
  p_backend bee.backend DEFAULT 'llm',
  p_prompt text DEFAULT NULL,
  p_model text DEFAULT NULL,
  p_output_schema jsonb DEFAULT '{}'::jsonb,
  p_backend_config jsonb DEFAULT '{}'::jsonb,
  p_config jsonb DEFAULT '{}'::jsonb
) RETURNS bigint
LANGUAGE plpgsql AS $$
DECLARE
  v_schema text;
  v_table text;
  v_pk text[];
  v_def_id bigint;
  v_version_id bigint;
  v_existing_type text;
  v_wanted_type text;
  def bee.column_def;
  c text;
BEGIN
  SELECT n.nspname, cl.relname INTO v_schema, v_table
  FROM pg_class cl JOIN pg_namespace n ON n.oid = cl.relnamespace
  WHERE cl.oid = p_table;
  v_pk := bee._pk_columns(p_table);
  IF v_pk IS NULL THEN
    RAISE EXCEPTION 'bee: table % has no primary key', p_table;
  END IF;
  IF p_source_columns IS NULL OR cardinality(p_source_columns) = 0 THEN
    RAISE EXCEPTION 'bee: at least one source column is required';
  END IF;
  FOREACH c IN ARRAY p_source_columns LOOP
    IF bee._column_type(p_table, c) IS NULL THEN
      RAISE EXCEPTION 'bee: source column % does not exist on %', c, p_table USING ERRCODE = 'undefined_column';
    END IF;
  END LOOP;
  IF p_column = ANY (p_source_columns) THEN
    RAISE EXCEPTION 'bee: target column % cannot be one of its sources', p_column;
  END IF;
  IF p_column = ANY (v_pk) THEN
    RAISE EXCEPTION 'bee: target column % is part of the primary key', p_column;
  END IF;
  PERFORM bee._validate_definition(p_output_type, p_backend, p_prompt, p_model, p_output_schema);

  v_wanted_type := bee._sql_type(p_output_type, p_output_schema);
  v_existing_type := bee._column_type(p_table, p_column);
  IF v_existing_type IS NULL THEN
    EXECUTE format('ALTER TABLE %I.%I ADD COLUMN %I %s', v_schema, v_table, p_column, v_wanted_type);
  ELSIF v_existing_type <> v_wanted_type THEN
    RAISE EXCEPTION 'bee: column % exists with type %, expected %', p_column, v_existing_type, v_wanted_type;
  END IF;

  INSERT INTO bee.column_def (table_schema, table_name, column_name, pk_columns, source_columns, output_type, config)
  VALUES (v_schema, v_table, p_column, v_pk, p_source_columns, p_output_type,
          bee._default_config() || coalesce(p_config, '{}'::jsonb))
  RETURNING id INTO v_def_id;

  INSERT INTO bee.column_version (column_def_id, version, backend, prompt, model, output_schema, backend_config)
  VALUES (v_def_id, 1, p_backend, p_prompt, p_model,
          coalesce(p_output_schema, '{}'::jsonb), coalesce(p_backend_config, '{}'::jsonb))
  RETURNING id INTO v_version_id;

  UPDATE bee.column_def SET current_version_id = v_version_id WHERE id = v_def_id
  RETURNING * INTO def;

  EXECUTE format(
    'CREATE TRIGGER %I AFTER INSERT OR UPDATE OF %s ON %I.%I FOR EACH ROW EXECUTE FUNCTION bee.enqueue_trigger(%L)',
    'bee_enqueue_' || p_column,
    (SELECT string_agg(format('%I', s), ', ') FROM unnest(p_source_columns) s),
    v_schema, v_table, v_def_id);
  EXECUTE format(
    'CREATE TRIGGER %I AFTER UPDATE OF %I ON %I.%I FOR EACH ROW EXECUTE FUNCTION bee.override_trigger(%L)',
    'bee_override_' || p_column, p_column, v_schema, v_table, v_def_id);
  PERFORM bee._install_forget_triggers(def);

  PERFORM bee.backfill(v_def_id);
  RETURN v_def_id;
END $$;

-- Same body as 0001 plus the forget triggers.
CREATE OR REPLACE FUNCTION bee.drop_column(p_table regclass, p_column text, p_drop_target boolean DEFAULT false) RETURNS void
LANGUAGE plpgsql AS $$
DECLARE
  def bee.column_def;
BEGIN
  def := bee._find_def(p_table, p_column);
  EXECUTE format('DROP TRIGGER IF EXISTS %I ON %I.%I', 'bee_enqueue_' || p_column, def.table_schema, def.table_name);
  EXECUTE format('DROP TRIGGER IF EXISTS %I ON %I.%I', 'bee_override_' || p_column, def.table_schema, def.table_name);
  EXECUTE format('DROP TRIGGER IF EXISTS %I ON %I.%I', 'bee_forget_' || p_column, def.table_schema, def.table_name);
  EXECUTE format('DROP TRIGGER IF EXISTS %I ON %I.%I', 'bee_forget_all_' || p_column, def.table_schema, def.table_name);
  DELETE FROM bee.job WHERE column_def_id = def.id AND status IN ('pending', 'claimed');
  UPDATE bee.column_def SET enabled = false, deleted_at = now(), updated_at = now() WHERE id = def.id;
  IF p_drop_target THEN
    EXECUTE format('ALTER TABLE %I.%I DROP COLUMN IF EXISTS %I', def.table_schema, def.table_name, p_column);
  END IF;
END $$;

-- Same body as 0014, with no value copied into the lineage for vector and halfvec columns.
-- SECURITY DEFINER and search_path repeated (0008): CREATE OR REPLACE resets them.
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
  v_lineage_value jsonb;
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
  -- A vector lives in its column; the lineage keeps who computed it, not a second copy.
  v_lineage_value := CASE WHEN def.output_type::text IN ('vector', 'halfvec') THEN NULL ELSE v_value END;

  -- The row changed, or a new version arrived, while the worker was computing: keep the value in
  -- the lineage under the version it was computed with, not current, and queue the row again.
  IF j.source_hash <> p_source_hash OR used.id <> ver.id THEN
    INSERT INTO bee.result (column_def_id, column_version_id, row_pk, source_hash, value, confidence, source,
                           is_current, written, model, usage, latency_ms, details)
    VALUES (def.id, used.id, j.row_pk, p_source_hash, v_lineage_value, p_confidence, 'model',
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
  VALUES (def.id, ver.id, j.row_pk, p_source_hash, v_lineage_value, p_confidence, 'model',
          true, v_written, coalesce(p_model, ver.model), p_usage, p_latency_ms, p_details);
  PERFORM bee._add_spend(def.id, p_usage);
  UPDATE bee.job SET status = 'done', last_error = NULL, updated_at = now() WHERE id = j.id;
  RETURN CASE WHEN v_written THEN 'written'::bee.complete_outcome ELSE 'held'::bee.complete_outcome END;
END $$;

-- The current definition of a derived column, without the queue counters of bee.columns: cheap
-- enough to read on every request (an application reading the embedding model of its queries).
CREATE OR REPLACE FUNCTION bee.definition(p_table regclass, p_column text)
RETURNS TABLE (
  id bigint,
  version integer,
  backend bee.backend,
  model text,
  prompt text,
  output_type bee.output_type,
  output_schema jsonb,
  backend_config jsonb,
  config jsonb,
  enabled boolean
)
LANGUAGE sql STABLE AS $$
  SELECT d.id, v.version, v.backend, v.model, v.prompt, d.output_type, v.output_schema,
         v.backend_config, d.config, d.enabled
  FROM bee._find_def(p_table, p_column) d
  JOIN bee.column_version v ON v.id = d.current_version_id;
$$;

COMMENT ON FUNCTION bee.definition(regclass, text) IS
  'The current definition of a derived column (version, backend, model, prompt, output schema, backend config, config). Raises if the column is not derived.';
COMMENT ON FUNCTION bee.forget_trigger() IS
  'Trigger on user tables: a deleted row or a truncated table leaves the current lineage and its live jobs are dropped.';
