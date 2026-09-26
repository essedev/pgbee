-- pgbee 0009: lineage retention.
-- bee.result keeps every value ever produced, and bee.job every completed job: both grow forever.
-- Each derived column can set lineage_retention_days; bee.prune deletes, in bounded batches, the
-- non current results older than that and the done jobs older than a week. Current results
-- (the value behind each cell, model or human) are never pruned, and bee.spend keeps the spend,
-- so budgets are unaffected. The reference worker calls bee.prune once an hour.

CREATE OR REPLACE FUNCTION bee._default_config() RETURNS jsonb
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
    "budget_period": "month",
    "backfill_chunk": 1000,
    "lineage_retention_days": null
  }'::jsonb;
$$;

CREATE OR REPLACE FUNCTION bee._validate_config() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  budget jsonb := NEW.config -> 'budget_usd';
  chunk jsonb := NEW.config -> 'backfill_chunk';
  retention jsonb := NEW.config -> 'lineage_retention_days';
BEGIN
  IF budget IS NOT NULL AND jsonb_typeof(budget) <> 'null'
     AND (jsonb_typeof(budget) <> 'number' OR (budget #>> '{}')::numeric < 0) THEN
    RAISE EXCEPTION 'bee: budget_usd must be a number >= 0 (USD) or null for no cap, got %', budget;
  END IF;
  IF NEW.config ? 'budget_period'
     AND NOT coalesce(NEW.config ->> 'budget_period' = ANY (ARRAY['day', 'month', 'total']), false) THEN
    RAISE EXCEPTION 'bee: budget_period must be day, month or total, got %', NEW.config -> 'budget_period';
  END IF;
  IF chunk IS NOT NULL AND (jsonb_typeof(chunk) <> 'number'
     OR (chunk #>> '{}')::numeric <> trunc((chunk #>> '{}')::numeric)
     OR (chunk #>> '{}')::numeric NOT BETWEEN 1 AND 100000) THEN
    RAISE EXCEPTION 'bee: backfill_chunk must be an integer between 1 and 100000, got %', chunk;
  END IF;
  IF retention IS NOT NULL AND jsonb_typeof(retention) <> 'null' AND (jsonb_typeof(retention) <> 'number'
     OR (retention #>> '{}')::numeric <> trunc((retention #>> '{}')::numeric)
     OR (retention #>> '{}')::numeric < 1) THEN
    RAISE EXCEPTION 'bee: lineage_retention_days must be an integer >= 1 or null to keep everything, got %', retention;
  END IF;
  RETURN NEW;
END $$;

CREATE INDEX IF NOT EXISTS result_history_idx
  ON bee.result (column_def_id, created_at) WHERE NOT is_current;
CREATE INDEX IF NOT EXISTS job_done_idx
  ON bee.job (updated_at) WHERE status = 'done';

CREATE OR REPLACE FUNCTION bee.prune(p_limit integer DEFAULT 10000, p_jobs_older_than interval DEFAULT '7 days')
RETURNS TABLE (results bigint, jobs bigint)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
  WITH doomed AS (
    SELECT r.id
    FROM bee.column_def d
    JOIN bee.result r ON r.column_def_id = d.id AND NOT r.is_current
    WHERE jsonb_typeof(d.config -> 'lineage_retention_days') = 'number'
      AND r.created_at < now() - make_interval(days => (d.config ->> 'lineage_retention_days')::integer)
    LIMIT p_limit
  ), del AS (
    DELETE FROM bee.result r USING doomed WHERE r.id = doomed.id RETURNING 1
  )
  SELECT count(*) INTO results FROM del;
  WITH doomed AS (
    SELECT j.id FROM bee.job j
    WHERE j.status = 'done' AND j.updated_at < now() - p_jobs_older_than
    LIMIT p_limit
  ), del AS (
    DELETE FROM bee.job j USING doomed WHERE j.id = doomed.id RETURNING 1
  )
  SELECT count(*) INTO jobs FROM del;
  RETURN NEXT;
END $$;
COMMENT ON FUNCTION bee.prune(integer, interval) IS
  'Worker contract (maintenance). Delete up to p_limit non current results older than their column lineage_retention_days, and up to p_limit done jobs older than p_jobs_older_than. Current results, dead jobs and bee.spend are kept. Call again while it returns p_limit.';

REVOKE EXECUTE ON FUNCTION bee.prune(integer, interval) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION bee.prune(integer, interval) TO bee_worker;
