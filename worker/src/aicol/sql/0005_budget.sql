-- ai-db 0005: spending cap per derived column.
-- Spend is counted per day from the usage the worker reports (usage.cost in USD) and checked by
-- claim_jobs: a column whose budget for the current period is used up gets no more jobs claimed.

CREATE TABLE IF NOT EXISTS ai.spend (
  column_def_id bigint NOT NULL REFERENCES ai.column_def (id),
  day           date NOT NULL,
  cost          numeric NOT NULL DEFAULT 0,
  results       bigint NOT NULL DEFAULT 0,
  PRIMARY KEY (column_def_id, day)
);
COMMENT ON TABLE ai.spend IS
  'Model spend per derived column per UTC day, fed by every model result (discarded ones too: they were paid). Kept when lineage is pruned.';

CREATE OR REPLACE FUNCTION ai._count_spend() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  INSERT INTO ai.spend AS s (column_def_id, day, cost, results)
  VALUES (NEW.column_def_id, (NEW.created_at AT TIME ZONE 'UTC')::date,
          coalesce((NEW.usage ->> 'cost')::numeric, 0), 1)
  ON CONFLICT (column_def_id, day)
  DO UPDATE SET cost = s.cost + excluded.cost, results = s.results + 1;
  RETURN NULL;
END $$;

DROP TRIGGER IF EXISTS ai_count_spend ON ai.result;
CREATE TRIGGER ai_count_spend AFTER INSERT ON ai.result
FOR EACH ROW WHEN (NEW.source = 'model') EXECUTE FUNCTION ai._count_spend();

INSERT INTO ai.spend (column_def_id, day, cost, results)
SELECT column_def_id, (created_at AT TIME ZONE 'UTC')::date,
       coalesce(sum((usage ->> 'cost')::numeric), 0), count(*)
FROM ai.result
WHERE source = 'model'
GROUP BY 1, 2
ON CONFLICT DO NOTHING;

CREATE OR REPLACE FUNCTION ai._period_start(p_period text) RETURNS date
LANGUAGE plpgsql STABLE AS $$
DECLARE
  today date := (now() AT TIME ZONE 'UTC')::date;
BEGIN
  RETURN CASE p_period
    WHEN 'day' THEN today
    WHEN 'month' THEN date_trunc('month', today)::date
    WHEN 'total' THEN '-infinity'::date
  END;
END $$;

CREATE OR REPLACE FUNCTION ai.spent(p_def_id bigint, p_period text DEFAULT 'total') RETURNS numeric
LANGUAGE sql STABLE AS $$
  SELECT coalesce(sum(cost), 0) FROM ai.spend
  WHERE column_def_id = p_def_id AND day >= ai._period_start(p_period);
$$;
COMMENT ON FUNCTION ai.spent(bigint, text) IS
  'USD spent by a derived column in the current UTC day, month, or in total.';

CREATE OR REPLACE FUNCTION ai._over_budget(p_def_id bigint, p_config jsonb) RETURNS boolean
LANGUAGE sql STABLE AS $$
  SELECT CASE
    WHEN jsonb_typeof(p_config -> 'budget_usd') IS DISTINCT FROM 'number' THEN false
    ELSE ai.spent(p_def_id, coalesce(p_config ->> 'budget_period', 'month'))
         >= (p_config ->> 'budget_usd')::numeric
  END;
$$;

CREATE OR REPLACE FUNCTION ai._default_config() RETURNS jsonb
LANGUAGE sql IMMUTABLE AS $$
  SELECT '{
    "batch_size": 20,
    "max_attempts": 5,
    "backoff_base_seconds": 30,
    "confidence_threshold": 0.7,
    "low_confidence_policy": "write",
    "override_policy": "pin",
    "concurrency": 4,
    "budget_usd": null,
    "budget_period": "month"
  }'::jsonb;
$$;

CREATE OR REPLACE FUNCTION ai._validate_config() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  budget jsonb := NEW.config -> 'budget_usd';
BEGIN
  IF budget IS NOT NULL AND jsonb_typeof(budget) <> 'null'
     AND (jsonb_typeof(budget) <> 'number' OR (budget #>> '{}')::numeric < 0) THEN
    RAISE EXCEPTION 'ai: budget_usd must be a number >= 0 (USD) or null for no cap, got %', budget;
  END IF;
  IF NEW.config ? 'budget_period'
     AND NOT coalesce(NEW.config ->> 'budget_period' = ANY (ARRAY['day', 'month', 'total']), false) THEN
    RAISE EXCEPTION 'ai: budget_period must be day, month or total, got %', NEW.config -> 'budget_period';
  END IF;
  RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS ai_validate_config ON ai.column_def;
CREATE TRIGGER ai_validate_config BEFORE INSERT OR UPDATE OF config ON ai.column_def
FOR EACH ROW EXECUTE FUNCTION ai._validate_config();

-- configure now wakes the workers: raising a budget (or the batch size) takes effect at once.
CREATE OR REPLACE FUNCTION ai.configure(p_table regclass, p_column text, p_config jsonb) RETURNS jsonb
LANGUAGE plpgsql AS $$
DECLARE
  def ai.column_def;
  merged jsonb;
BEGIN
  def := ai._find_def(p_table, p_column);
  UPDATE ai.column_def SET config = config || p_config, updated_at = now()
  WHERE id = def.id RETURNING config INTO merged;
  PERFORM pg_notify('ai_jobs', def.id::text);
  RETURN merged;
END $$;
COMMENT ON FUNCTION ai.configure(regclass, text, jsonb) IS
  'Merge operational settings (batch_size, max_attempts, backoff_base_seconds, confidence_threshold, low_confidence_policy, override_policy, concurrency, budget_usd, budget_period) without creating a version.';

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
    WITH open_defs AS (
      -- Budget checked once per definition, not per job.
      SELECT d.id
      FROM ai.column_def d
      JOIN ai.column_version v ON v.id = d.current_version_id
      WHERE d.enabled AND d.deleted_at IS NULL
        AND (p_backends IS NULL OR v.backend = ANY (p_backends))
        AND NOT ai._over_budget(d.id, d.config)
    ), picked AS (
      SELECT jb.id
      FROM ai.job jb
      JOIN open_defs od ON od.id = jb.column_def_id
      WHERE jb.status = 'pending' AND jb.next_attempt_at <= now()
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
  'Worker contract. Lock up to batch_size ready jobs (SKIP LOCKED), mark them claimed and return each with its source values and current version. Optionally restricted to some backends. Columns over budget are skipped.';

CREATE OR REPLACE VIEW ai.budgets AS
SELECT d.id AS column_def_id, d.table_schema, d.table_name, d.column_name,
       b.budget_usd, b.budget_period, b.spent_usd, ai.spent(d.id, 'total') AS spent_total_usd,
       b.budget_usd - b.spent_usd AS remaining_usd,
       coalesce(b.spent_usd >= b.budget_usd, false) AS exhausted
FROM ai.column_def d
CROSS JOIN LATERAL (
  SELECT CASE WHEN jsonb_typeof(d.config -> 'budget_usd') = 'number'
              THEN (d.config ->> 'budget_usd')::numeric END AS budget_usd,
         coalesce(d.config ->> 'budget_period', 'month') AS budget_period,
         ai.spent(d.id, coalesce(d.config ->> 'budget_period', 'month')) AS spent_usd
) b
WHERE d.deleted_at IS NULL;
