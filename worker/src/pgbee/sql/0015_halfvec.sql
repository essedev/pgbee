-- pgbee 0015: output type halfvec, half-precision vectors (pgvector 0.7 or later).
-- Half the storage of vector, and HNSW indexes up to 4000 dimensions instead of 2000, which is what
-- 3072-dimension embedding models need. Produced by the embedding backend like vector.
--
-- The new enum value cannot be used in the transaction that adds it, and the install applies each
-- file in one transaction. So the SQL-language functions below compare output_type as text: their
-- bodies are parsed when created, and an enum literal would be resolved right here. The PL/pgSQL
-- ones are parsed on first call, in a later transaction.

ALTER TYPE bee.output_type ADD VALUE IF NOT EXISTS 'halfvec' AFTER 'vector';

-- Same body as 0001 plus halfvec.
CREATE OR REPLACE FUNCTION bee._sql_type(p_output_type bee.output_type, p_output_schema jsonb) RETURNS text
LANGUAGE sql IMMUTABLE AS $$
  SELECT CASE p_output_type::text
    WHEN 'enum' THEN 'text'
    WHEN 'text' THEN 'text'
    WHEN 'boolean' THEN 'boolean'
    WHEN 'integer' THEN 'integer'
    WHEN 'numeric' THEN 'numeric'
    WHEN 'jsonb' THEN 'jsonb'
    WHEN 'vector' THEN format('vector(%s)', (p_output_schema ->> 'dimensions')::integer)
    WHEN 'halfvec' THEN format('halfvec(%s)', (p_output_schema ->> 'dimensions')::integer)
  END;
$$;

-- Same body as 0008 plus halfvec, qualified with the schema pgvector lives in.
CREATE OR REPLACE FUNCTION bee._cast_expr(p_output_type bee.output_type, p_param text) RETURNS text
LANGUAGE sql STABLE AS $$
  SELECT CASE p_output_type::text
    WHEN 'enum' THEN format('(%s #>> ''{}'')', p_param)
    WHEN 'text' THEN format('(%s #>> ''{}'')', p_param)
    WHEN 'boolean' THEN format('(%s #>> ''{}'')::boolean', p_param)
    WHEN 'integer' THEN format('(%s #>> ''{}'')::integer', p_param)
    WHEN 'numeric' THEN format('(%s #>> ''{}'')::numeric', p_param)
    WHEN 'jsonb' THEN p_param
    WHEN 'vector' THEN format('(%s #>> ''{}'')::%I.vector', p_param,
      (SELECT n.nspname FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace
       WHERE e.extname = 'vector'))
    WHEN 'halfvec' THEN format('(%s #>> ''{}'')::%I.halfvec', p_param,
      (SELECT n.nspname FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace
       WHERE e.extname = 'vector'))
  END;
$$;

-- Same body as 0004, with halfvec wherever vector is accepted.
CREATE OR REPLACE FUNCTION bee._validate_definition(
  p_output_type bee.output_type, p_backend bee.backend, p_prompt text, p_model text, p_output_schema jsonb
) RETURNS void
LANGUAGE plpgsql STABLE AS $$
DECLARE
  v_values jsonb;
  v_is_vector boolean := p_output_type::text IN ('vector', 'halfvec');
BEGIN
  IF p_model IS NULL OR p_model = '' THEN
    RAISE EXCEPTION 'bee: model is required (for backend custom it names the worker)';
  END IF;
  IF p_backend IN ('llm', 'decision') THEN
    IF p_prompt IS NULL OR p_prompt = '' THEN
      RAISE EXCEPTION 'bee: backend % requires a prompt', p_backend;
    END IF;
    IF v_is_vector THEN
      RAISE EXCEPTION 'bee: backend % cannot produce a vector, use backend embedding', p_backend;
    END IF;
  ELSIF p_backend = 'embedding' AND NOT v_is_vector THEN
    RAISE EXCEPTION 'bee: backend embedding produces a vector or halfvec, got output_type %', p_output_type;
  END IF;
  IF p_backend = 'decision' THEN
    IF p_output_type NOT IN ('enum', 'boolean', 'integer', 'numeric') THEN
      RAISE EXCEPTION 'bee: backend decision answers typed questions only (enum, boolean, integer, numeric), got %', p_output_type;
    END IF;
    IF p_output_type = 'enum' AND jsonb_typeof(p_output_schema) <> 'object' THEN
      RAISE EXCEPTION 'bee: backend decision needs enum criteria as {"value": "description"}';
    END IF;
    IF p_output_type IN ('integer', 'numeric') AND (
         jsonb_typeof(p_output_schema -> 'levels') IS DISTINCT FROM 'array'
         OR jsonb_array_length(p_output_schema -> 'levels') NOT BETWEEN 2 AND 10) THEN
      RAISE EXCEPTION 'bee: backend decision scores on a rubric: output_schema {"levels": [2 to 10 descriptions]}';
    END IF;
  END IF;
  IF p_output_type = 'enum' THEN
    v_values := bee._enum_values(p_output_schema);
    IF jsonb_array_length(v_values) = 0
       OR EXISTS (SELECT 1 FROM jsonb_array_elements(v_values) e WHERE jsonb_typeof(e) <> 'string')
       OR (jsonb_typeof(p_output_schema) = 'object'
           AND EXISTS (SELECT 1 FROM jsonb_each(p_output_schema) kv WHERE jsonb_typeof(kv.value) <> 'string')) THEN
      RAISE EXCEPTION 'bee: output_type enum requires output_schema as a non-empty array of strings or an object {"value": "description"}';
    END IF;
  END IF;
  IF v_is_vector THEN
    IF coalesce((p_output_schema ->> 'dimensions')::integer, 0) <= 0 THEN
      RAISE EXCEPTION 'bee: output_type % requires output_schema {"dimensions": N}', p_output_type;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector') THEN
      RAISE EXCEPTION 'bee: output_type % requires the pgvector extension (CREATE EXTENSION vector)', p_output_type;
    END IF;
    IF p_output_type::text = 'halfvec' AND NOT EXISTS (
         SELECT 1 FROM pg_extension e JOIN pg_type t ON t.typnamespace = e.extnamespace
         WHERE e.extname = 'vector' AND t.typname = 'halfvec') THEN
      RAISE EXCEPTION 'bee: output_type halfvec requires pgvector 0.7 or later (ALTER EXTENSION vector UPDATE)';
    END IF;
  END IF;
END $$;

-- Same body as 0004, with halfvec checked like vector.
CREATE OR REPLACE FUNCTION bee._validate_value(p_output_type bee.output_type, p_output_schema jsonb, p_value jsonb) RETURNS void
LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE
  t text := jsonb_typeof(p_value);
BEGIN
  IF p_value IS NULL THEN RETURN; END IF;
  CASE p_output_type::text
    WHEN 'enum' THEN
      IF t <> 'string' OR NOT (bee._enum_values(p_output_schema) @> jsonb_build_array(p_value)) THEN
        RAISE EXCEPTION 'bee: value % is not one of %', p_value, bee._enum_values(p_output_schema) USING ERRCODE = 'check_violation';
      END IF;
    WHEN 'text' THEN
      IF t <> 'string' THEN
        RAISE EXCEPTION 'bee: expected a string, got %', t USING ERRCODE = 'check_violation';
      END IF;
    WHEN 'boolean' THEN
      IF t <> 'boolean' THEN
        RAISE EXCEPTION 'bee: expected a boolean, got %', t USING ERRCODE = 'check_violation';
      END IF;
    WHEN 'integer' THEN
      IF t <> 'number' OR (p_value #>> '{}')::numeric <> trunc((p_value #>> '{}')::numeric) THEN
        RAISE EXCEPTION 'bee: expected an integer, got %', p_value USING ERRCODE = 'check_violation';
      END IF;
    WHEN 'numeric' THEN
      IF t <> 'number' THEN
        RAISE EXCEPTION 'bee: expected a number, got %', t USING ERRCODE = 'check_violation';
      END IF;
    WHEN 'jsonb' THEN
      NULL;
    WHEN 'vector', 'halfvec' THEN
      IF t <> 'array' OR jsonb_array_length(p_value) <> (p_output_schema ->> 'dimensions')::integer THEN
        RAISE EXCEPTION 'bee: expected an array of % numbers', p_output_schema ->> 'dimensions' USING ERRCODE = 'check_violation';
      END IF;
  END CASE;
END $$;

-- Same body as 0003, with the dimension guard extended to halfvec.
CREATE OR REPLACE FUNCTION bee.update_column(
  p_table regclass,
  p_column text,
  p_prompt text DEFAULT NULL,
  p_model text DEFAULT NULL,
  p_output_schema jsonb DEFAULT NULL,
  p_backend_config jsonb DEFAULT NULL
) RETURNS bigint
LANGUAGE plpgsql AS $$
DECLARE
  def bee.column_def;
  prev bee.column_version;
  v_version_id bigint;
  v_prompt text;
  v_model text;
  v_schema jsonb;
  v_backend_config jsonb;
BEGIN
  def := bee._find_def(p_table, p_column);
  PERFORM 1 FROM bee.column_def WHERE id = def.id FOR UPDATE;
  SELECT * INTO prev FROM bee.column_version WHERE id = def.current_version_id;
  v_prompt := coalesce(p_prompt, prev.prompt);
  v_model := coalesce(p_model, prev.model);
  v_schema := coalesce(p_output_schema, prev.output_schema);
  v_backend_config := coalesce(p_backend_config, prev.backend_config);
  IF v_prompt IS NOT DISTINCT FROM prev.prompt AND v_model = prev.model
     AND v_schema = prev.output_schema AND v_backend_config = prev.backend_config THEN
    RAISE EXCEPTION 'bee: nothing changed for % on %', p_column, p_table;
  END IF;
  PERFORM bee._validate_definition(def.output_type, prev.backend, v_prompt, v_model, v_schema);
  IF def.output_type::text IN ('vector', 'halfvec')
     AND (v_schema ->> 'dimensions') <> (prev.output_schema ->> 'dimensions') THEN
    RAISE EXCEPTION 'bee: vector dimensions cannot change, drop and re-add the column';
  END IF;
  INSERT INTO bee.column_version (column_def_id, version, backend, prompt, model, output_schema, backend_config)
  VALUES (def.id, prev.version + 1, prev.backend, v_prompt, v_model, v_schema, v_backend_config)
  RETURNING id INTO v_version_id;
  UPDATE bee.column_def SET current_version_id = v_version_id, updated_at = now() WHERE id = def.id;
  UPDATE bee.job SET attempts = 0, next_attempt_at = now(), last_error = NULL, updated_at = now()
  WHERE column_def_id = def.id AND status = 'pending';
  PERFORM bee.backfill(def.id);
  RETURN v_version_id;
END $$;
