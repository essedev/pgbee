-- pgbee 0012: the comment of bee.configure lists every configuration key (it is what \df+ shows).

COMMENT ON FUNCTION bee.configure(regclass, text, jsonb) IS
  'Merge operational settings without creating a version: batch_size, max_attempts, backoff_base_seconds, confidence_threshold, low_confidence_policy (write, hold), override_policy (pin, until_source_change), concurrency, budget_usd, budget_period (day, month, total), backfill_chunk, lineage_retention_days. Returns the merged config.';
