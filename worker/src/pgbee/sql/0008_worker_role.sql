-- pgbee 0008: a role for workers with the least privilege.
-- A worker needs the four contract functions and nothing else: no access to the user tables,
-- no direct writes to bee.job. The contract functions and the triggers run as the owner of the
-- extension (SECURITY DEFINER) with a fixed search_path, so the worker role and the application
-- roles that write the user tables need no grants on the bee schema tables. EXECUTE on the
-- contract is revoked from PUBLIC: a SECURITY DEFINER function callable by anyone would let any
-- role read source columns (claim_jobs) or write target columns (complete_job).
--
-- Give a worker its login with:  CREATE ROLE pgbee LOGIN PASSWORD '...' IN ROLE bee_worker;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bee_worker') THEN
    CREATE ROLE bee_worker NOLOGIN;
  END IF;
EXCEPTION WHEN insufficient_privilege THEN
  RAISE EXCEPTION 'bee: creating the role bee_worker needs CREATEROLE. Create it once as a superuser (CREATE ROLE bee_worker NOLOGIN) and run the install again';
END $$;

-- The cast to vector must not depend on the caller's search_path: qualify it with the schema
-- pgvector lives in.
CREATE OR REPLACE FUNCTION bee._cast_expr(p_output_type bee.output_type, p_param text) RETURNS text
LANGUAGE sql STABLE AS $$
  SELECT CASE p_output_type
    WHEN 'enum' THEN format('(%s #>> ''{}'')', p_param)
    WHEN 'text' THEN format('(%s #>> ''{}'')', p_param)
    WHEN 'boolean' THEN format('(%s #>> ''{}'')::boolean', p_param)
    WHEN 'integer' THEN format('(%s #>> ''{}'')::integer', p_param)
    WHEN 'numeric' THEN format('(%s #>> ''{}'')::numeric', p_param)
    WHEN 'jsonb' THEN p_param
    WHEN 'vector' THEN format('(%s #>> ''{}'')::%I.vector', p_param,
      (SELECT n.nspname FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace
       WHERE e.extname = 'vector'))
  END;
$$;

ALTER FUNCTION bee.claim_jobs(text, integer, bee.backend[])
  SECURITY DEFINER SET search_path = pg_catalog, pg_temp;
ALTER FUNCTION bee.complete_job(bigint, bytea, jsonb, real, text, jsonb, integer, jsonb)
  SECURITY DEFINER SET search_path = pg_catalog, pg_temp;
ALTER FUNCTION bee.fail_job(bigint, text, boolean)
  SECURITY DEFINER SET search_path = pg_catalog, pg_temp;
ALTER FUNCTION bee.reclaim_stale(interval)
  SECURITY DEFINER SET search_path = pg_catalog, pg_temp;
ALTER FUNCTION bee.enqueue_trigger()
  SECURITY DEFINER SET search_path = pg_catalog, pg_temp;
ALTER FUNCTION bee.override_trigger()
  SECURITY DEFINER SET search_path = pg_catalog, pg_temp;

REVOKE EXECUTE ON FUNCTION
  bee.claim_jobs(text, integer, bee.backend[]),
  bee.complete_job(bigint, bytea, jsonb, real, text, jsonb, integer, jsonb),
  bee.fail_job(bigint, text, boolean),
  bee.reclaim_stale(interval)
FROM PUBLIC;
GRANT USAGE ON SCHEMA bee TO bee_worker;
GRANT EXECUTE ON FUNCTION
  bee.claim_jobs(text, integer, bee.backend[]),
  bee.complete_job(bigint, bytea, jsonb, real, text, jsonb, integer, jsonb),
  bee.fail_job(bigint, text, boolean),
  bee.reclaim_stale(interval)
TO bee_worker;
-- Read only views for `pgbee status` and for operators running under the worker role.
GRANT SELECT ON bee.columns, bee.budgets, bee.dead_jobs, bee.needs_review, bee.stale_rows,
  bee.cost_by_column TO bee_worker;
