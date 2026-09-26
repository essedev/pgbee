-- pgbee 0013: token prices per column, for providers that do not report a cost.
-- OpenRouter reports the cost of every call; OpenAI, Azure and local servers (Ollama, vLLM)
-- report only tokens. With input_usd_per_mtok and output_usd_per_mtok in the config, the worker
-- prices the tokens itself, so spend, budgets and bee.cost_by_column work with any provider. The
-- price is an operational setting (bee.configure, no new version): after update_column changes
-- the model, the prices are updated with configure.

-- Same body as 0009 plus the two prices.
CREATE OR REPLACE FUNCTION bee._validate_config() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  budget jsonb := NEW.config -> 'budget_usd';
  chunk jsonb := NEW.config -> 'backfill_chunk';
  retention jsonb := NEW.config -> 'lineage_retention_days';
  price_key text;
  price jsonb;
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
  FOREACH price_key IN ARRAY ARRAY['input_usd_per_mtok', 'output_usd_per_mtok'] LOOP
    price := NEW.config -> price_key;
    IF price IS NOT NULL AND jsonb_typeof(price) <> 'null'
       AND (jsonb_typeof(price) <> 'number' OR (price #>> '{}')::numeric < 0) THEN
      RAISE EXCEPTION 'bee: % must be a number >= 0 (USD per million tokens) or null, got %', price_key, price;
    END IF;
  END LOOP;
  RETURN NEW;
END $$;

COMMENT ON FUNCTION bee.configure(regclass, text, jsonb) IS
  'Merge operational settings without creating a version: batch_size, max_attempts, backoff_base_seconds, confidence_threshold, low_confidence_policy (write, hold), override_policy (pin, until_source_change), concurrency, budget_usd, budget_period (day, month, total), backfill_chunk, lineage_retention_days, input_usd_per_mtok, output_usd_per_mtok. Returns the merged config.';
