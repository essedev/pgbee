-- ai-db 0003: a new version gives pending jobs a fresh attempt budget and no backoff.
-- A job waiting after failures under the old prompt or model should run again right away with the new one.

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
  UPDATE ai.job SET attempts = 0, next_attempt_at = now(), last_error = NULL, updated_at = now()
  WHERE column_def_id = def.id AND status = 'pending';
  PERFORM ai.backfill(def.id);
  RETURN v_version_id;
END $$;
COMMENT ON FUNCTION ai.update_column(regclass, text, text, text, jsonb, jsonb) IS
  'Create a new version of a derived column (prompt, model, output schema or backend config) and enqueue only the rows computed with an older version. Pending jobs get a fresh attempt budget. Returns the new version id.';
