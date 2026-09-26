-- ai-db 0001: schema, catalog, queue, lineage, triggers, worker contract.
-- Applied once by `aicol install`; tracked in ai.schema_version.

CREATE SCHEMA IF NOT EXISTS ai;

DO $$ BEGIN
  CREATE TYPE ai.output_type AS ENUM ('enum', 'text', 'boolean', 'integer', 'numeric', 'jsonb', 'vector');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
  CREATE TYPE ai.backend AS ENUM ('llm', 'embedding', 'custom');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
  CREATE TYPE ai.job_status AS ENUM ('pending', 'claimed', 'done', 'dead');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
  CREATE TYPE ai.result_source AS ENUM ('model', 'human');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
  CREATE TYPE ai.complete_outcome AS ENUM ('written', 'held', 'stale_requeued', 'cancelled');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;


-- Tables

CREATE TABLE IF NOT EXISTS ai.schema_version (
  version    integer PRIMARY KEY,
  applied_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS ai.column_def (
  id                 bigserial PRIMARY KEY,
  table_schema       text NOT NULL,
  table_name         text NOT NULL,
  column_name        text NOT NULL,
  pk_columns         text[] NOT NULL,
  source_columns     text[] NOT NULL,
  output_type        ai.output_type NOT NULL,
  current_version_id bigint,
  config             jsonb NOT NULL DEFAULT '{}'::jsonb,
  enabled            boolean NOT NULL DEFAULT true,
  created_at         timestamptz NOT NULL DEFAULT now(),
  updated_at         timestamptz NOT NULL DEFAULT now(),
  deleted_at         timestamptz
);

CREATE UNIQUE INDEX IF NOT EXISTS column_def_live_key
  ON ai.column_def (table_schema, table_name, column_name) WHERE deleted_at IS NULL;

CREATE TABLE IF NOT EXISTS ai.column_version (
  id             bigserial PRIMARY KEY,
  column_def_id  bigint NOT NULL REFERENCES ai.column_def (id),
  version        integer NOT NULL,
  backend        ai.backend NOT NULL,
  prompt         text,
  model          text NOT NULL,
  output_schema  jsonb NOT NULL DEFAULT '{}'::jsonb,
  backend_config jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at     timestamptz NOT NULL DEFAULT now(),
  UNIQUE (column_def_id, version)
);

DO $$ BEGIN
  ALTER TABLE ai.column_def
    ADD CONSTRAINT column_def_current_version_fk
    FOREIGN KEY (current_version_id) REFERENCES ai.column_version (id);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE TABLE IF NOT EXISTS ai.job (
  id              bigserial PRIMARY KEY,
  column_def_id   bigint NOT NULL REFERENCES ai.column_def (id),
  row_pk          jsonb NOT NULL,
  source_hash     bytea NOT NULL,
  status          ai.job_status NOT NULL DEFAULT 'pending',
  attempts        integer NOT NULL DEFAULT 0,
  next_attempt_at timestamptz NOT NULL DEFAULT now(),
  claimed_by      text,
  claimed_at      timestamptz,
  last_error      text,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS job_live_key
  ON ai.job (column_def_id, row_pk) WHERE status IN ('pending', 'claimed');
CREATE INDEX IF NOT EXISTS job_claim_idx
  ON ai.job (next_attempt_at, id) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS job_claimed_idx
  ON ai.job (claimed_at) WHERE status = 'claimed';

CREATE TABLE IF NOT EXISTS ai.result (
  id                bigserial PRIMARY KEY,
  column_def_id     bigint NOT NULL REFERENCES ai.column_def (id),
  column_version_id bigint REFERENCES ai.column_version (id),
  row_pk            jsonb NOT NULL,
  source_hash       bytea NOT NULL,
  value             jsonb,
  confidence        real,
  source            ai.result_source NOT NULL,
  is_current        boolean NOT NULL DEFAULT true,
  written           boolean NOT NULL DEFAULT true,
  model             text,
  usage             jsonb,
  latency_ms        integer,
  created_at        timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS result_current_key
  ON ai.result (column_def_id, row_pk) WHERE is_current;
CREATE INDEX IF NOT EXISTS result_version_idx
  ON ai.result (column_def_id, column_version_id) WHERE is_current;


-- Internal helpers

CREATE OR REPLACE FUNCTION ai._default_config() RETURNS jsonb
LANGUAGE sql IMMUTABLE AS $$
  SELECT '{
    "batch_size": 20,
    "max_attempts": 5,
    "backoff_base_seconds": 30,
    "confidence_threshold": 0.7,
    "low_confidence_policy": "write",
    "override_policy": "pin",
    "concurrency": 4
  }'::jsonb;
$$;

CREATE OR REPLACE FUNCTION ai._pk_columns(p_table regclass) RETURNS text[]
LANGUAGE sql STABLE AS $$
  SELECT array_agg(a.attname::text ORDER BY k.ord)
  FROM pg_index i
  CROSS JOIN LATERAL unnest(i.indkey) WITH ORDINALITY AS k(attnum, ord)
  JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.attnum
  WHERE i.indrelid = p_table AND i.indisprimary;
$$;

CREATE OR REPLACE FUNCTION ai._column_type(p_table regclass, p_column text) RETURNS text
LANGUAGE sql STABLE AS $$
  SELECT format_type(a.atttypid, a.atttypmod)
  FROM pg_attribute a
  WHERE a.attrelid = p_table AND a.attname = p_column AND a.attnum > 0 AND NOT a.attisdropped;
$$;

CREATE OR REPLACE FUNCTION ai._row_pk(p_rec jsonb, p_pk_columns text[]) RETURNS jsonb
LANGUAGE sql IMMUTABLE AS $$
  SELECT jsonb_object_agg(k, p_rec -> k) FROM unnest(p_pk_columns) AS k;
$$;

CREATE OR REPLACE FUNCTION ai._source_hash(p_rec jsonb, p_source_columns text[]) RETURNS bytea
LANGUAGE sql IMMUTABLE AS $$
  SELECT sha256(convert_to(
    (SELECT jsonb_object_agg(k, p_rec -> k) FROM unnest(p_source_columns) AS k)::text, 'UTF8'));
$$;

-- WHERE clause matching a user row by its primary key, with $1 bound to the row_pk jsonb.
CREATE OR REPLACE FUNCTION ai._pk_where(p_def ai.column_def, p_alias text DEFAULT 't') RETURNS text
LANGUAGE sql STABLE AS $$
  SELECT string_agg(
    format('%s.%I = ($1 ->> %L)::%s', p_alias, k, k,
           ai._column_type(format('%I.%I', p_def.table_schema, p_def.table_name)::regclass, k)),
    ' AND ')
  FROM unnest(p_def.pk_columns) AS k;
$$;

CREATE OR REPLACE FUNCTION ai._find_def(p_table regclass, p_column text) RETURNS ai.column_def
LANGUAGE plpgsql STABLE AS $$
DECLARE
  def ai.column_def;
BEGIN
  SELECT d.* INTO def
  FROM ai.column_def d
  JOIN pg_class c ON c.relname = d.table_name
  JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = d.table_schema
  WHERE c.oid = p_table AND d.column_name = p_column AND d.deleted_at IS NULL;
  IF def.id IS NULL THEN
    RAISE EXCEPTION 'ai: no derived column % on %', p_column, p_table USING ERRCODE = 'undefined_column';
  END IF;
  RETURN def;
END $$;

CREATE OR REPLACE FUNCTION ai._is_pinned(p_def_id bigint, p_row_pk jsonb) RETURNS boolean
LANGUAGE sql STABLE AS $$
  SELECT EXISTS (
    SELECT 1 FROM ai.result r
    WHERE r.column_def_id = p_def_id AND r.row_pk = p_row_pk AND r.is_current AND r.source = 'human');
$$;

CREATE OR REPLACE FUNCTION ai._sql_type(p_output_type ai.output_type, p_output_schema jsonb) RETURNS text
LANGUAGE sql IMMUTABLE AS $$
  SELECT CASE p_output_type
    WHEN 'enum' THEN 'text'
    WHEN 'text' THEN 'text'
    WHEN 'boolean' THEN 'boolean'
    WHEN 'integer' THEN 'integer'
    WHEN 'numeric' THEN 'numeric'
    WHEN 'jsonb' THEN 'jsonb'
    WHEN 'vector' THEN format('vector(%s)', (p_output_schema ->> 'dimensions')::integer)
  END;
$$;

-- SQL expression casting a jsonb parameter (named by p_param) to the target column type.
CREATE OR REPLACE FUNCTION ai._cast_expr(p_output_type ai.output_type, p_param text) RETURNS text
LANGUAGE sql IMMUTABLE AS $$
  SELECT CASE p_output_type
    WHEN 'enum' THEN format('(%s #>> ''{}'')', p_param)
    WHEN 'text' THEN format('(%s #>> ''{}'')', p_param)
    WHEN 'boolean' THEN format('(%s #>> ''{}'')::boolean', p_param)
    WHEN 'integer' THEN format('(%s #>> ''{}'')::integer', p_param)
    WHEN 'numeric' THEN format('(%s #>> ''{}'')::numeric', p_param)
    WHEN 'jsonb' THEN p_param
    WHEN 'vector' THEN format('(%s #>> ''{}'')::vector', p_param)
  END;
$$;

CREATE OR REPLACE FUNCTION ai._validate_definition(
  p_output_type ai.output_type, p_backend ai.backend, p_prompt text, p_model text, p_output_schema jsonb
) RETURNS void
LANGUAGE plpgsql STABLE AS $$
BEGIN
  IF p_model IS NULL OR p_model = '' THEN
    RAISE EXCEPTION 'ai: model is required (for backend custom it names the worker)';
  END IF;
  IF p_backend = 'llm' THEN
    IF p_prompt IS NULL OR p_prompt = '' THEN
      RAISE EXCEPTION 'ai: backend llm requires a prompt';
    END IF;
    IF p_output_type = 'vector' THEN
      RAISE EXCEPTION 'ai: backend llm cannot produce a vector, use backend embedding';
    END IF;
  ELSIF p_backend = 'embedding' AND p_output_type <> 'vector' THEN
    RAISE EXCEPTION 'ai: backend embedding produces a vector, got output_type %', p_output_type;
  END IF;
  IF p_output_type = 'enum' THEN
    IF jsonb_typeof(p_output_schema) <> 'array' OR jsonb_array_length(p_output_schema) = 0
       OR EXISTS (SELECT 1 FROM jsonb_array_elements(p_output_schema) e WHERE jsonb_typeof(e) <> 'string') THEN
      RAISE EXCEPTION 'ai: output_type enum requires output_schema as a non-empty array of strings';
    END IF;
  END IF;
  IF p_output_type = 'vector' THEN
    IF coalesce((p_output_schema ->> 'dimensions')::integer, 0) <= 0 THEN
      RAISE EXCEPTION 'ai: output_type vector requires output_schema {"dimensions": N}';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector') THEN
      RAISE EXCEPTION 'ai: output_type vector requires the pgvector extension (CREATE EXTENSION vector)';
    END IF;
  END IF;
END $$;

CREATE OR REPLACE FUNCTION ai._validate_value(p_output_type ai.output_type, p_output_schema jsonb, p_value jsonb) RETURNS void
LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE
  t text := jsonb_typeof(p_value);
BEGIN
  IF p_value IS NULL THEN RETURN; END IF;
  CASE p_output_type
    WHEN 'enum' THEN
      IF t <> 'string' OR NOT (p_output_schema @> jsonb_build_array(p_value)) THEN
        RAISE EXCEPTION 'ai: value % is not one of %', p_value, p_output_schema USING ERRCODE = 'check_violation';
      END IF;
    WHEN 'text' THEN
      IF t <> 'string' THEN
        RAISE EXCEPTION 'ai: expected a string, got %', t USING ERRCODE = 'check_violation';
      END IF;
    WHEN 'boolean' THEN
      IF t <> 'boolean' THEN
        RAISE EXCEPTION 'ai: expected a boolean, got %', t USING ERRCODE = 'check_violation';
      END IF;
    WHEN 'integer' THEN
      IF t <> 'number' OR (p_value #>> '{}')::numeric <> trunc((p_value #>> '{}')::numeric) THEN
        RAISE EXCEPTION 'ai: expected an integer, got %', p_value USING ERRCODE = 'check_violation';
      END IF;
    WHEN 'numeric' THEN
      IF t <> 'number' THEN
        RAISE EXCEPTION 'ai: expected a number, got %', t USING ERRCODE = 'check_violation';
      END IF;
    WHEN 'jsonb' THEN
      NULL;
    WHEN 'vector' THEN
      IF t <> 'array' OR jsonb_array_length(p_value) <> (p_output_schema ->> 'dimensions')::integer THEN
        RAISE EXCEPTION 'ai: expected an array of % numbers', p_output_schema ->> 'dimensions' USING ERRCODE = 'check_violation';
      END IF;
  END CASE;
END $$;

CREATE OR REPLACE FUNCTION ai._enqueue(p_def_id bigint, p_row_pk jsonb, p_hash bytea) RETURNS void
LANGUAGE plpgsql AS $$
BEGIN
  INSERT INTO ai.job (column_def_id, row_pk, source_hash)
  VALUES (p_def_id, p_row_pk, p_hash)
  ON CONFLICT (column_def_id, row_pk) WHERE status IN ('pending', 'claimed')
  DO UPDATE SET source_hash = EXCLUDED.source_hash, updated_at = now();
  PERFORM pg_notify('ai_jobs', p_def_id::text);
END $$;

CREATE OR REPLACE FUNCTION ai._current_row(p_def ai.column_def, p_row_pk jsonb) RETURNS jsonb
LANGUAGE plpgsql STABLE AS $$
DECLARE
  rec jsonb;
BEGIN
  EXECUTE format('SELECT to_jsonb(t) FROM %I.%I t WHERE %s',
                 p_def.table_schema, p_def.table_name, ai._pk_where(p_def))
  INTO rec USING p_row_pk;
  RETURN rec;
END $$;


-- Triggers installed on user tables

CREATE OR REPLACE FUNCTION ai.enqueue_trigger() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  def ai.column_def;
  rec jsonb := to_jsonb(NEW);
  pk jsonb;
  h bytea;
BEGIN
  SELECT * INTO def FROM ai.column_def WHERE id = TG_ARGV[0]::bigint;
  IF def.id IS NULL OR NOT def.enabled OR def.deleted_at IS NOT NULL THEN
    RETURN NULL;
  END IF;
  h := ai._source_hash(rec, def.source_columns);
  IF TG_OP = 'UPDATE' AND h = ai._source_hash(to_jsonb(OLD), def.source_columns) THEN
    RETURN NULL;
  END IF;
  pk := ai._row_pk(rec, def.pk_columns);
  IF ai._is_pinned(def.id, pk) AND coalesce(def.config ->> 'override_policy', 'pin') = 'pin' THEN
    RETURN NULL;
  END IF;
  IF EXISTS (
    SELECT 1 FROM ai.result r
    WHERE r.column_def_id = def.id AND r.row_pk = pk AND r.is_current
      AND r.source_hash = h AND r.column_version_id = def.current_version_id
  ) THEN
    RETURN NULL;
  END IF;
  PERFORM ai._enqueue(def.id, pk, h);
  RETURN NULL;
END $$;

CREATE OR REPLACE FUNCTION ai.override_trigger() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  def ai.column_def;
  new_rec jsonb := to_jsonb(NEW);
  old_rec jsonb := to_jsonb(OLD);
  new_val jsonb;
  pk jsonb;
  h bytea;
BEGIN
  IF coalesce(current_setting('ai.writer', true), '') = 'worker' THEN
    RETURN NULL;
  END IF;
  SELECT * INTO def FROM ai.column_def WHERE id = TG_ARGV[0]::bigint;
  IF def.id IS NULL OR def.deleted_at IS NOT NULL THEN
    RETURN NULL;
  END IF;
  new_val := new_rec -> def.column_name;
  IF new_val IS NOT DISTINCT FROM (old_rec -> def.column_name) THEN
    RETURN NULL;
  END IF;
  pk := ai._row_pk(new_rec, def.pk_columns);
  h := ai._source_hash(new_rec, def.source_columns);
  UPDATE ai.result SET is_current = false
  WHERE column_def_id = def.id AND row_pk = pk AND is_current;
  IF jsonb_typeof(new_val) = 'null' THEN
    -- Setting the target to NULL by hand asks for a recompute: the pin is gone, enqueue.
    IF def.enabled THEN
      PERFORM ai._enqueue(def.id, pk, h);
    END IF;
    RETURN NULL;
  END IF;
  INSERT INTO ai.result (column_def_id, column_version_id, row_pk, source_hash, value, source, is_current, written)
  VALUES (def.id, NULL, pk, h, new_val, 'human', true, true);
  DELETE FROM ai.job
  WHERE column_def_id = def.id AND row_pk = pk AND status IN ('pending', 'claimed');
  RETURN NULL;
END $$;


-- Management API

CREATE OR REPLACE FUNCTION ai.backfill(p_def_id bigint) RETURNS bigint
LANGUAGE plpgsql AS $$
DECLARE
  def ai.column_def;
  n bigint;
BEGIN
  SELECT * INTO def FROM ai.column_def WHERE id = p_def_id AND deleted_at IS NULL;
  IF def.id IS NULL THEN
    RAISE EXCEPTION 'ai: derived column % does not exist', p_def_id;
  END IF;
  EXECUTE format($q$
    WITH rows AS (
      SELECT ai._row_pk(to_jsonb(t), $1) AS pk, ai._source_hash(to_jsonb(t), $2) AS h
      FROM %I.%I t
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
    SELECT count(*) FROM ins
  $q$, def.table_schema, def.table_name)
  INTO n
  USING def.pk_columns, def.source_columns, def.id, def.current_version_id;
  IF n > 0 THEN
    PERFORM pg_notify('ai_jobs', def.id::text);
  END IF;
  RETURN n;
END $$;
COMMENT ON FUNCTION ai.backfill(bigint) IS
  'Enqueue every row of the table whose current value is missing, stale or computed on different sources. Skips human-pinned rows. Returns the number of jobs enqueued.';

CREATE OR REPLACE FUNCTION ai.add_column(
  p_table regclass,
  p_column text,
  p_source_columns text[],
  p_output_type ai.output_type,
  p_backend ai.backend DEFAULT 'llm',
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
  c text;
BEGIN
  SELECT n.nspname, cl.relname INTO v_schema, v_table
  FROM pg_class cl JOIN pg_namespace n ON n.oid = cl.relnamespace
  WHERE cl.oid = p_table;
  v_pk := ai._pk_columns(p_table);
  IF v_pk IS NULL THEN
    RAISE EXCEPTION 'ai: table % has no primary key', p_table;
  END IF;
  IF p_source_columns IS NULL OR cardinality(p_source_columns) = 0 THEN
    RAISE EXCEPTION 'ai: at least one source column is required';
  END IF;
  FOREACH c IN ARRAY p_source_columns LOOP
    IF ai._column_type(p_table, c) IS NULL THEN
      RAISE EXCEPTION 'ai: source column % does not exist on %', c, p_table USING ERRCODE = 'undefined_column';
    END IF;
  END LOOP;
  IF p_column = ANY (p_source_columns) THEN
    RAISE EXCEPTION 'ai: target column % cannot be one of its sources', p_column;
  END IF;
  IF p_column = ANY (v_pk) THEN
    RAISE EXCEPTION 'ai: target column % is part of the primary key', p_column;
  END IF;
  PERFORM ai._validate_definition(p_output_type, p_backend, p_prompt, p_model, p_output_schema);

  v_wanted_type := ai._sql_type(p_output_type, p_output_schema);
  v_existing_type := ai._column_type(p_table, p_column);
  IF v_existing_type IS NULL THEN
    EXECUTE format('ALTER TABLE %I.%I ADD COLUMN %I %s', v_schema, v_table, p_column, v_wanted_type);
  ELSIF v_existing_type <> v_wanted_type THEN
    RAISE EXCEPTION 'ai: column % exists with type %, expected %', p_column, v_existing_type, v_wanted_type;
  END IF;

  INSERT INTO ai.column_def (table_schema, table_name, column_name, pk_columns, source_columns, output_type, config)
  VALUES (v_schema, v_table, p_column, v_pk, p_source_columns, p_output_type,
          ai._default_config() || coalesce(p_config, '{}'::jsonb))
  RETURNING id INTO v_def_id;

  INSERT INTO ai.column_version (column_def_id, version, backend, prompt, model, output_schema, backend_config)
  VALUES (v_def_id, 1, p_backend, p_prompt, p_model,
          coalesce(p_output_schema, '{}'::jsonb), coalesce(p_backend_config, '{}'::jsonb))
  RETURNING id INTO v_version_id;

  UPDATE ai.column_def SET current_version_id = v_version_id WHERE id = v_def_id;

  EXECUTE format(
    'CREATE TRIGGER %I AFTER INSERT OR UPDATE OF %s ON %I.%I FOR EACH ROW EXECUTE FUNCTION ai.enqueue_trigger(%L)',
    'ai_enqueue_' || p_column,
    (SELECT string_agg(format('%I', s), ', ') FROM unnest(p_source_columns) s),
    v_schema, v_table, v_def_id);
  EXECUTE format(
    'CREATE TRIGGER %I AFTER UPDATE OF %I ON %I.%I FOR EACH ROW EXECUTE FUNCTION ai.override_trigger(%L)',
    'ai_override_' || p_column, p_column, v_schema, v_table, v_def_id);

  PERFORM ai.backfill(v_def_id);
  RETURN v_def_id;
END $$;
COMMENT ON FUNCTION ai.add_column(regclass, text, text[], ai.output_type, ai.backend, text, text, jsonb, jsonb, jsonb) IS
  'Declare a derived column: adds the target column if missing, installs the triggers, records version 1 and enqueues existing rows. Returns the definition id.';

CREATE OR REPLACE FUNCTION ai.update_column(
  p_table regclass,
  p_column text,
  p_prompt text DEFAULT NULL,
  p_model text DEFAULT NULL,
  p_output_schema jsonb DEFAULT NULL,
  p_backend_config jsonb DEFAULT NULL
) RETURNS bigint
LANGUAGE plpgsql AS $$
DECLARE
  def ai.column_def;
  prev ai.column_version;
  v_version_id bigint;
  v_prompt text;
  v_model text;
  v_schema jsonb;
  v_backend_config jsonb;
BEGIN
  def := ai._find_def(p_table, p_column);
  PERFORM 1 FROM ai.column_def WHERE id = def.id FOR UPDATE;
  SELECT * INTO prev FROM ai.column_version WHERE id = def.current_version_id;
  v_prompt := coalesce(p_prompt, prev.prompt);
  v_model := coalesce(p_model, prev.model);
  v_schema := coalesce(p_output_schema, prev.output_schema);
  v_backend_config := coalesce(p_backend_config, prev.backend_config);
  IF v_prompt IS NOT DISTINCT FROM prev.prompt AND v_model = prev.model
     AND v_schema = prev.output_schema AND v_backend_config = prev.backend_config THEN
    RAISE EXCEPTION 'ai: nothing changed for % on %', p_column, p_table;
  END IF;
  PERFORM ai._validate_definition(def.output_type, prev.backend, v_prompt, v_model, v_schema);
  IF def.output_type = 'vector' AND (v_schema ->> 'dimensions') <> (prev.output_schema ->> 'dimensions') THEN
    RAISE EXCEPTION 'ai: vector dimensions cannot change, drop and re-add the column';
  END IF;
  INSERT INTO ai.column_version (column_def_id, version, backend, prompt, model, output_schema, backend_config)
  VALUES (def.id, prev.version + 1, prev.backend, v_prompt, v_model, v_schema, v_backend_config)
  RETURNING id INTO v_version_id;
  UPDATE ai.column_def SET current_version_id = v_version_id, updated_at = now() WHERE id = def.id;
  PERFORM ai.backfill(def.id);
  RETURN v_version_id;
END $$;
COMMENT ON FUNCTION ai.update_column(regclass, text, text, text, jsonb, jsonb) IS
  'Create a new version of a derived column (prompt, model, output schema or backend config) and enqueue only the rows computed with an older version. Returns the new version id.';

CREATE OR REPLACE FUNCTION ai.configure(p_table regclass, p_column text, p_config jsonb) RETURNS jsonb
LANGUAGE plpgsql AS $$
DECLARE
  def ai.column_def;
  merged jsonb;
BEGIN
  def := ai._find_def(p_table, p_column);
  UPDATE ai.column_def SET config = config || p_config, updated_at = now()
  WHERE id = def.id RETURNING config INTO merged;
  RETURN merged;
END $$;
COMMENT ON FUNCTION ai.configure(regclass, text, jsonb) IS
  'Merge operational settings (batch_size, max_attempts, backoff_base_seconds, confidence_threshold, low_confidence_policy, override_policy, concurrency) without creating a version.';

CREATE OR REPLACE FUNCTION ai.enable(p_table regclass, p_column text) RETURNS bigint
LANGUAGE plpgsql AS $$
DECLARE
  def ai.column_def;
BEGIN
  def := ai._find_def(p_table, p_column);
  UPDATE ai.column_def SET enabled = true, updated_at = now() WHERE id = def.id;
  RETURN ai.backfill(def.id);
END $$;
COMMENT ON FUNCTION ai.enable(regclass, text) IS
  'Resume a derived column and enqueue the rows changed while it was disabled. Returns the number of jobs enqueued.';

CREATE OR REPLACE FUNCTION ai.disable(p_table regclass, p_column text) RETURNS void
LANGUAGE plpgsql AS $$
DECLARE
  def ai.column_def;
BEGIN
  def := ai._find_def(p_table, p_column);
  UPDATE ai.column_def SET enabled = false, updated_at = now() WHERE id = def.id;
  DELETE FROM ai.job WHERE column_def_id = def.id AND status = 'pending';
END $$;
COMMENT ON FUNCTION ai.disable(regclass, text) IS
  'Pause a derived column: triggers stay but stop enqueuing, pending jobs are dropped. Claimed jobs finish normally.';

CREATE OR REPLACE FUNCTION ai.drop_column(p_table regclass, p_column text, p_drop_target boolean DEFAULT false) RETURNS void
LANGUAGE plpgsql AS $$
DECLARE
  def ai.column_def;
BEGIN
  def := ai._find_def(p_table, p_column);
  EXECUTE format('DROP TRIGGER IF EXISTS %I ON %I.%I', 'ai_enqueue_' || p_column, def.table_schema, def.table_name);
  EXECUTE format('DROP TRIGGER IF EXISTS %I ON %I.%I', 'ai_override_' || p_column, def.table_schema, def.table_name);
  DELETE FROM ai.job WHERE column_def_id = def.id AND status IN ('pending', 'claimed');
  UPDATE ai.column_def SET enabled = false, deleted_at = now(), updated_at = now() WHERE id = def.id;
  IF p_drop_target THEN
    EXECUTE format('ALTER TABLE %I.%I DROP COLUMN IF EXISTS %I', def.table_schema, def.table_name, p_column);
  END IF;
END $$;
COMMENT ON FUNCTION ai.drop_column(regclass, text, boolean) IS
  'Stop deriving a column: removes the triggers and live jobs, soft-deletes the definition, keeps the lineage. Drops the target column only when asked.';

CREATE OR REPLACE FUNCTION ai.unpin(p_table regclass, p_column text, p_row_pk jsonb) RETURNS boolean
LANGUAGE plpgsql AS $$
DECLARE
  def ai.column_def;
  rec jsonb;
  n integer;
BEGIN
  def := ai._find_def(p_table, p_column);
  UPDATE ai.result SET is_current = false
  WHERE column_def_id = def.id AND row_pk = p_row_pk AND is_current AND source = 'human';
  GET DIAGNOSTICS n = ROW_COUNT;
  IF n = 0 THEN
    RETURN false;
  END IF;
  rec := ai._current_row(def, p_row_pk);
  IF rec IS NOT NULL AND def.enabled THEN
    PERFORM ai._enqueue(def.id, p_row_pk, ai._source_hash(rec, def.source_columns));
  END IF;
  RETURN true;
END $$;
COMMENT ON FUNCTION ai.unpin(regclass, text, jsonb) IS
  'Release a human override on one row (row_pk as jsonb, e.g. {"id": 42}) and enqueue it for recompute. Returns false when the row was not pinned.';

CREATE OR REPLACE FUNCTION ai.retry_dead(p_table regclass, p_column text) RETURNS integer
LANGUAGE plpgsql AS $$
DECLARE
  def ai.column_def;
  n integer;
BEGIN
  def := ai._find_def(p_table, p_column);
  DELETE FROM ai.job d
  WHERE d.column_def_id = def.id AND d.status = 'dead'
    AND EXISTS (SELECT 1 FROM ai.job l WHERE l.column_def_id = d.column_def_id AND l.row_pk = d.row_pk
                AND l.status IN ('pending', 'claimed'));
  UPDATE ai.job SET status = 'pending', attempts = 0, next_attempt_at = now(), updated_at = now()
  WHERE column_def_id = def.id AND status = 'dead';
  GET DIAGNOSTICS n = ROW_COUNT;
  IF n > 0 THEN
    PERFORM pg_notify('ai_jobs', def.id::text);
  END IF;
  RETURN n;
END $$;
COMMENT ON FUNCTION ai.retry_dead(regclass, text) IS
  'Put the dead jobs of a derived column back in the queue with a fresh attempt budget. Returns how many.';

CREATE OR REPLACE FUNCTION ai.prune_jobs(p_older_than interval DEFAULT '7 days') RETURNS bigint
LANGUAGE plpgsql AS $$
DECLARE
  n bigint;
BEGIN
  DELETE FROM ai.job WHERE status = 'done' AND updated_at < now() - p_older_than;
  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN n;
END $$;
COMMENT ON FUNCTION ai.prune_jobs(interval) IS
  'Delete completed jobs older than the interval. Lineage in ai.result is untouched.';


-- Worker contract

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
  FOR j IN
    WITH picked AS (
      SELECT jb.id
      FROM ai.job jb
      JOIN ai.column_def d ON d.id = jb.column_def_id
      JOIN ai.column_version v ON v.id = d.current_version_id
      WHERE jb.status = 'pending' AND jb.next_attempt_at <= now()
        AND d.enabled AND d.deleted_at IS NULL
        AND (p_backends IS NULL OR v.backend = ANY (p_backends))
      ORDER BY jb.next_attempt_at, jb.id
      LIMIT p_batch_size
      FOR UPDATE OF jb SKIP LOCKED
    ), claimed AS (
      UPDATE ai.job jb
      SET status = 'claimed', claimed_by = p_worker_id, claimed_at = now(),
          attempts = jb.attempts + 1, updated_at = now()
      FROM picked
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
END $$;
COMMENT ON FUNCTION ai.claim_jobs(text, integer, ai.backend[]) IS
  'Worker contract. Lock up to batch_size ready jobs (SKIP LOCKED), mark them claimed and return each with its source values and current version. Optionally restricted to some backends.';

CREATE OR REPLACE FUNCTION ai.complete_job(
  p_job_id bigint,
  p_source_hash bytea,
  p_value jsonb,
  p_confidence real DEFAULT NULL,
  p_model text DEFAULT NULL,
  p_usage jsonb DEFAULT NULL,
  p_latency_ms integer DEFAULT NULL
) RETURNS ai.complete_outcome
LANGUAGE plpgsql AS $$
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
    -- The row changed while the worker was computing: keep the result as history, run again.
    INSERT INTO ai.result (column_def_id, column_version_id, row_pk, source_hash, value, confidence, source,
                           is_current, written, model, usage, latency_ms)
    VALUES (def.id, ver.id, j.row_pk, p_source_hash, v_value, p_confidence, 'model',
            false, false, coalesce(p_model, ver.model), p_usage, p_latency_ms);
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
  IF ver.backend = 'llm' AND p_confidence IS NOT NULL AND v_threshold IS NOT NULL
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
                         is_current, written, model, usage, latency_ms)
  VALUES (def.id, ver.id, j.row_pk, p_source_hash, v_value, p_confidence, 'model',
          true, v_written, coalesce(p_model, ver.model), p_usage, p_latency_ms);
  UPDATE ai.job SET status = 'done', last_error = NULL, updated_at = now() WHERE id = j.id;
  RETURN CASE WHEN v_written THEN 'written'::ai.complete_outcome ELSE 'held'::ai.complete_outcome END;
END $$;
COMMENT ON FUNCTION ai.complete_job(bigint, bytea, jsonb, real, text, jsonb, integer) IS
  'Worker contract. Store a computed value: validates it, writes the target column unless held by the low-confidence policy, records lineage, closes the job. Requeues if the row changed meanwhile.';

CREATE OR REPLACE FUNCTION ai.fail_job(p_job_id bigint, p_error text, p_retryable boolean DEFAULT true) RETURNS ai.job_status
LANGUAGE plpgsql AS $$
DECLARE
  j ai.job;
  def ai.column_def;
  v_max integer;
  v_base numeric;
BEGIN
  SELECT * INTO j FROM ai.job WHERE id = p_job_id FOR UPDATE;
  IF j.id IS NULL THEN
    RETURN NULL;
  END IF;
  IF j.status <> 'claimed' THEN
    RETURN j.status;
  END IF;
  SELECT * INTO def FROM ai.column_def WHERE id = j.column_def_id;
  v_max := coalesce((def.config ->> 'max_attempts')::integer, 5);
  v_base := coalesce((def.config ->> 'backoff_base_seconds')::numeric, 30);
  IF NOT p_retryable OR j.attempts >= v_max THEN
    UPDATE ai.job SET status = 'dead', last_error = p_error, claimed_by = NULL, claimed_at = NULL, updated_at = now()
    WHERE id = j.id;
    RETURN 'dead';
  END IF;
  UPDATE ai.job
  SET status = 'pending', last_error = p_error, claimed_by = NULL, claimed_at = NULL, updated_at = now(),
      next_attempt_at = now() + make_interval(secs => (v_base * power(2, j.attempts - 1))::double precision)
  WHERE id = j.id;
  RETURN 'pending';
END $$;
COMMENT ON FUNCTION ai.fail_job(bigint, text, boolean) IS
  'Worker contract. Record a failure: retryable errors go back to pending with exponential backoff until max_attempts, others (or exhausted ones) become dead.';

CREATE OR REPLACE FUNCTION ai.reclaim_stale(p_timeout interval DEFAULT '5 minutes') RETURNS integer
LANGUAGE plpgsql AS $$
DECLARE
  n integer;
BEGIN
  UPDATE ai.job j
  SET status = CASE WHEN j.attempts >= coalesce((d.config ->> 'max_attempts')::integer, 5)
                    THEN 'dead'::ai.job_status ELSE 'pending'::ai.job_status END,
      last_error = format('reclaimed: claimed by %s at %s and never completed', j.claimed_by, j.claimed_at),
      claimed_by = NULL, claimed_at = NULL, updated_at = now()
  FROM ai.column_def d
  WHERE d.id = j.column_def_id AND j.status = 'claimed' AND j.claimed_at < now() - p_timeout;
  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN n;
END $$;
COMMENT ON FUNCTION ai.reclaim_stale(interval) IS
  'Worker contract. Return to the queue the jobs claimed longer than the timeout ago (worker died). Jobs out of attempts become dead. Returns how many were touched.';


-- Views

CREATE OR REPLACE VIEW ai.columns AS
SELECT d.id, d.table_schema, d.table_name, d.column_name, d.source_columns, d.output_type, d.enabled,
       v.version, v.backend, v.model, v.prompt, v.output_schema, v.backend_config, d.config,
       (SELECT count(*) FROM ai.job j WHERE j.column_def_id = d.id AND j.status = 'pending') AS pending,
       (SELECT count(*) FROM ai.job j WHERE j.column_def_id = d.id AND j.status = 'claimed') AS claimed,
       (SELECT count(*) FROM ai.job j WHERE j.column_def_id = d.id AND j.status = 'done') AS done,
       (SELECT count(*) FROM ai.job j WHERE j.column_def_id = d.id AND j.status = 'dead') AS dead,
       (SELECT count(*) FROM ai.result r WHERE r.column_def_id = d.id AND r.is_current AND r.source = 'human') AS human_overrides,
       (SELECT count(*) FROM ai.result r WHERE r.column_def_id = d.id AND r.is_current AND r.source = 'model'
          AND r.column_version_id <> d.current_version_id) AS stale
FROM ai.column_def d
JOIN ai.column_version v ON v.id = d.current_version_id
WHERE d.deleted_at IS NULL;

CREATE OR REPLACE VIEW ai.stale_rows AS
SELECT d.id AS column_def_id, d.table_schema, d.table_name, d.column_name, r.row_pk,
       r.column_version_id AS result_version_id, d.current_version_id
FROM ai.result r
JOIN ai.column_def d ON d.id = r.column_def_id
WHERE r.is_current AND r.source = 'model' AND r.column_version_id <> d.current_version_id AND d.deleted_at IS NULL;

CREATE OR REPLACE VIEW ai.needs_review AS
SELECT d.id AS column_def_id, d.table_schema, d.table_name, d.column_name, r.row_pk,
       r.value, r.confidence, r.written, r.model, r.created_at
FROM ai.result r
JOIN ai.column_def d ON d.id = r.column_def_id
WHERE r.is_current AND r.source = 'model' AND d.deleted_at IS NULL
  AND r.confidence IS NOT NULL AND r.confidence < (d.config ->> 'confidence_threshold')::real;

CREATE OR REPLACE VIEW ai.dead_jobs AS
SELECT j.id AS job_id, d.table_schema, d.table_name, d.column_name, j.row_pk, j.attempts, j.last_error, j.updated_at
FROM ai.job j
JOIN ai.column_def d ON d.id = j.column_def_id
WHERE j.status = 'dead';

CREATE OR REPLACE VIEW ai.cost_by_column AS
SELECT d.id AS column_def_id, d.table_schema, d.table_name, d.column_name, r.column_version_id, r.model,
       count(*) AS results,
       sum((r.usage ->> 'prompt_tokens')::bigint) AS prompt_tokens,
       sum((r.usage ->> 'completion_tokens')::bigint) AS completion_tokens,
       sum((r.usage ->> 'cost')::numeric) AS cost,
       avg(r.latency_ms)::integer AS avg_latency_ms
FROM ai.result r
JOIN ai.column_def d ON d.id = r.column_def_id
WHERE r.source = 'model'
GROUP BY d.id, d.table_schema, d.table_name, d.column_name, r.column_version_id, r.model;
