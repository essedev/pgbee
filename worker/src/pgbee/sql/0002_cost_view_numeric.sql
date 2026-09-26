-- pgbee 0002: usage is split across the jobs of one embedding call, so token counts can be fractional.

DROP VIEW IF EXISTS bee.cost_by_column;

CREATE VIEW bee.cost_by_column AS
SELECT d.id AS column_def_id, d.table_schema, d.table_name, d.column_name, r.column_version_id, r.model,
       count(*) AS results,
       round(sum((r.usage ->> 'prompt_tokens')::numeric))::bigint AS prompt_tokens,
       round(sum((r.usage ->> 'completion_tokens')::numeric))::bigint AS completion_tokens,
       sum((r.usage ->> 'cost')::numeric) AS cost,
       avg(r.latency_ms)::integer AS avg_latency_ms
FROM bee.result r
JOIN bee.column_def d ON d.id = r.column_def_id
WHERE r.source = 'model'
GROUP BY d.id, d.table_schema, d.table_name, d.column_name, r.column_version_id, r.model;
