-- pgbee 0006: claim_jobs brings along the decision siblings of the rows it picks.
-- The worker asks all the decision questions on a row in one call (the state is paid once).
-- During a backfill the jobs of different columns sit far apart in the queue, so without this
-- they would land in different batches and never share a call.

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
LANGUAGE plpgsql AS $$
DECLARE
  j record;
  def bee.column_def;
  src jsonb;
BEGIN
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
          attempts = jb.attempts + 1, updated_at = now()
      FROM (SELECT id FROM picked UNION SELECT id FROM siblings) picked
      WHERE jb.id = picked.id
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
END $$;
COMMENT ON FUNCTION bee.claim_jobs(text, integer, bee.backend[]) IS
  'Worker contract. Lock up to batch_size ready jobs (SKIP LOCKED), mark them claimed and return each with its source values and current version. Optionally restricted to some backends. Columns over budget are skipped. Decision jobs bring along the ready decision jobs of the same rows and model, so a batch can exceed batch_size.';

