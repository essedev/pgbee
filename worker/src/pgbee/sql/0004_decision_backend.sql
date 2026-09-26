-- pgbee 0004: backend `decision` (typed decision models such as TypeSafe Jev), enum criteria with
-- descriptions, per-result details (class probabilities).

ALTER TYPE bee.backend ADD VALUE IF NOT EXISTS 'decision';

ALTER TABLE bee.result ADD COLUMN IF NOT EXISTS details jsonb;
COMMENT ON COLUMN bee.result.details IS
  'Backend specific extras for the current value: class probabilities for decision models, etc.';

-- An enum output_schema is either an array of values or an object {value: description}.
-- Descriptions are optional for llm and required by decision models (they are the criteria).
CREATE OR REPLACE FUNCTION bee._enum_values(p_output_schema jsonb) RETURNS jsonb
LANGUAGE sql IMMUTABLE AS $$
  SELECT CASE jsonb_typeof(p_output_schema)
    WHEN 'array' THEN p_output_schema
    WHEN 'object' THEN (SELECT coalesce(jsonb_agg(k), '[]'::jsonb) FROM jsonb_object_keys(p_output_schema) k)
    ELSE '[]'::jsonb
  END;
$$;

CREATE OR REPLACE FUNCTION bee._validate_definition(
  p_output_type bee.output_type, p_backend bee.backend, p_prompt text, p_model text, p_output_schema jsonb
) RETURNS void
LANGUAGE plpgsql STABLE AS $$
DECLARE
  v_values jsonb;
BEGIN
  IF p_model IS NULL OR p_model = '' THEN
    RAISE EXCEPTION 'bee: model is required (for backend custom it names the worker)';
  END IF;
  IF p_backend IN ('llm', 'decision') THEN
    IF p_prompt IS NULL OR p_prompt = '' THEN
      RAISE EXCEPTION 'bee: backend % requires a prompt', p_backend;
    END IF;
    IF p_output_type = 'vector' THEN
      RAISE EXCEPTION 'bee: backend % cannot produce a vector, use backend embedding', p_backend;
    END IF;
  ELSIF p_backend = 'embedding' AND p_output_type <> 'vector' THEN
    RAISE EXCEPTION 'bee: backend embedding produces a vector, got output_type %', p_output_type;
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
  IF p_output_type = 'vector' THEN
    IF coalesce((p_output_schema ->> 'dimensions')::integer, 0) <= 0 THEN
      RAISE EXCEPTION 'bee: output_type vector requires output_schema {"dimensions": N}';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector') THEN
      RAISE EXCEPTION 'bee: output_type vector requires the pgvector extension (CREATE EXTENSION vector)';
    END IF;
  END IF;
END $$;

CREATE OR REPLACE FUNCTION bee._validate_value(p_output_type bee.output_type, p_output_schema jsonb, p_value jsonb) RETURNS void
LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE
  t text := jsonb_typeof(p_value);
BEGIN
  IF p_value IS NULL THEN RETURN; END IF;
  CASE p_output_type
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
    WHEN 'vector' THEN
      IF t <> 'array' OR jsonb_array_length(p_value) <> (p_output_schema ->> 'dimensions')::integer THEN
        RAISE EXCEPTION 'bee: expected an array of % numbers', p_output_schema ->> 'dimensions' USING ERRCODE = 'check_violation';
      END IF;
  END CASE;
END $$;

-- complete_job gains an optional trailing p_details. Same behaviour for existing callers.
DROP FUNCTION IF EXISTS bee.complete_job(bigint, bytea, jsonb, real, text, jsonb, integer);

CREATE FUNCTION bee.complete_job(
  p_job_id bigint,
  p_source_hash bytea,
  p_value jsonb,
  p_confidence real DEFAULT NULL,
  p_model text DEFAULT NULL,
  p_usage jsonb DEFAULT NULL,
  p_latency_ms integer DEFAULT NULL,
  p_details jsonb DEFAULT NULL
) RETURNS bee.complete_outcome
LANGUAGE plpgsql AS $$
DECLARE
  j bee.job;
  def bee.column_def;
  ver bee.column_version;
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
  IF v_value IS NOT NULL AND jsonb_typeof(v_value) = 'null' THEN
    v_value := NULL;
  END IF;
  PERFORM bee._validate_value(def.output_type, ver.output_schema, v_value);

  IF j.source_hash <> p_source_hash THEN
    INSERT INTO bee.result (column_def_id, column_version_id, row_pk, source_hash, value, confidence, source,
                           is_current, written, model, usage, latency_ms, details)
    VALUES (def.id, ver.id, j.row_pk, p_source_hash, v_value, p_confidence, 'model',
            false, false, coalesce(p_model, ver.model), p_usage, p_latency_ms, p_details);
    UPDATE bee.job SET status = 'pending', claimed_by = NULL, claimed_at = NULL,
                      next_attempt_at = now(), updated_at = now()
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
  UPDATE bee.job SET status = 'done', last_error = NULL, updated_at = now() WHERE id = j.id;
  RETURN CASE WHEN v_written THEN 'written'::bee.complete_outcome ELSE 'held'::bee.complete_outcome END;
END $$;
COMMENT ON FUNCTION bee.complete_job(bigint, bytea, jsonb, real, text, jsonb, integer, jsonb) IS
  'Worker contract. Store a computed value: validates it, writes the target column unless held by the low-confidence policy, records lineage (with optional details such as class probabilities), closes the job. Requeues if the row changed meanwhile.';

CREATE OR REPLACE VIEW bee.needs_review AS
SELECT d.id AS column_def_id, d.table_schema, d.table_name, d.column_name, r.row_pk,
       r.value, r.confidence, r.written, r.model, r.created_at
FROM bee.result r
JOIN bee.column_def d ON d.id = r.column_def_id
WHERE r.is_current AND r.source = 'model' AND d.deleted_at IS NULL
  AND r.confidence IS NOT NULL AND r.confidence < (d.config ->> 'confidence_threshold')::real;
