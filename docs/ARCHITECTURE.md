# Architecture

## Overview

pgbee adds model-derived columns to PostgreSQL. You declare once that `ticket.urgency` is derived from `ticket.body` with a prompt and a model, and from then on every new or changed row gets its value without application code. The database owns the guarantees (transactional queue, retries, versions, lineage, human overrides, budgets); an external process makes the model calls. Market context and competition are in `ANALYSIS.md` (Italian).

## Components

- **SQL extension** (`sql/`): the `bee` schema with the catalog of derived columns, versions, job queue, result lineage, triggers on user tables and the worker contract functions. Plain SQL and PL/pgSQL, no compiled code: it installs on PostgreSQL 15 to 18, managed services included, with `pgbee install`, or as a real extension (`CREATE EXTENSION pgbee`) where files can be copied into the server's `sharedir`. The files live in `worker/src/pgbee/sql/` and ship inside the worker's wheel and Docker image (`worker/Dockerfile`, non-root user, configuration from the environment only). It is the source of truth.
- **Worker** (`worker/`): a Python process (package `pgbee`) with a CLI. It installs and upgrades the extension (`pgbee install`), writes the files for `CREATE EXTENSION` (`pgbee extension-files`), runs the work loop (`pgbee run`) and shows the state (`pgbee status`). It talks to models through OpenRouter or through any OpenAI-compatible endpoint (OpenAI, Azure, Ollama, vLLM), one provider per process (decision #22). It does not know the user's tables: jobs carry the source values already read by the database, and the worker returns values.
- **Quickstart** (`compose.yml`, `examples/quickstart.sql`): Postgres, a one-shot install and the worker in one `docker compose up`, plus a table of eight tickets with three derived columns.
- **Demo** (`demo/`): an Italian support-ticket table with six derived columns (four `llm`, one `decision`, one `embedding`); `run.py` exercises the full cycle in eight steps and `compare_models.py` measures accuracy, cost and latency of several models against `gold.json`. `cfpb/` is the field test on real text: `prepare.py` extracts 3000 CFPB complaints stratified by product, `field_test.py` loads them into a dedicated database, declares four columns and runs the real `pgbee run` process, then measures agreement, calibration, cost and throughput (results in `cfpb/results*.json`, reading in `ANALYSIS.md`).

## The life of a row

1. `INSERT INTO ticket (body) VALUES (...)`. The trigger installed by `bee.add_column` hashes the source column values (SHA-256) and inserts a `pending` job with the row's primary key and the hash. If a live job already exists for the same row and column, it updates that job's hash instead. All of this happens in the insert's transaction: either the row goes in with its job, or nothing does.
2. The worker runs as a pool with one lane per backend (`llm`, `decision`, `embedding`), each on its own connection, so a fast backend never waits for a slow one to finish its batch. `pgbee run --backends` picks the lanes a process serves, to scale backends on separate processes or machines; only the first lane does maintenance and reclaims abandoned jobs. Each lane wakes on `NOTIFY bee_jobs` (or on its poll interval) and calls `bee.claim_jobs(worker_id, batch_size, backends)` for its backend. The function picks ready jobs with `FOR UPDATE SKIP LOCKED`, marks them `claimed` together with the current version, and returns each with its source values (read with dynamic SQL) and the version's prompt, model and output schema. The worker groups jobs by definition and sends requests in parallel, within the definition's `concurrency`; `decision` jobs are grouped by row and model across definitions (see Backends). Notifications received while a lane works are all consumed at the next wake-up; `SIGINT` and `SIGTERM` let the current batch finish and interrupt waits immediately.
3. The worker calls the model with structured output (a JSON schema derived from the declared type) and gets `{value, confidence}`.
4. `bee.complete_job(job_id, source_hash, value, confidence, model, usage, latency_ms, details)` checks that the processed hash is still the job's (the row may have changed meanwhile) and that the version is still current, validates the value against the type, writes the target column with dynamic SQL inside a guard (`SET LOCAL bee.writer = 'worker'`) so the override trigger does not take it for a human correction, records the result in the lineage as current, adds the cost to `bee.spend` and closes the job. If the hash or the version changed while the worker was computing, the result goes to the lineage as not current, under the version it was actually computed with, and the job goes back to `pending`.
5. On error the worker calls `bee.fail_job(job_id, error, retryable)`: a retryable error under the attempt limit sends the job back to `pending` with exponential backoff; otherwise it becomes `dead`, visible in `bee.dead_jobs` and recoverable with `bee.retry_dead`. A worker that dies leaves `claimed` jobs behind: `bee.reclaim_stale` returns them to the queue after the timeout.

## Backfill

`bee.add_column`, `bee.update_column` and `bee.enable` call `bee.backfill`, which enqueues the first chunk (`backfill_chunk`, default 1000 rows in primary key order), stores a cursor and returns. `bee.claim_jobs` enqueues the rest: before and after claiming, for each column with a running backfill, it adds chunks until the column has at least `backfill_chunk` pending jobs (at most ten chunks per claim), and sends a `NOTIFY` while the scan is not over, so the worker comes back without waiting for its poll. Rows inserted or changed during the scan are enqueued by the triggers as usual. Disabled columns and columns over budget do not advance; `pgbee status` shows the scanned rows while the scan runs. On 1M rows `add_column` went from 39 s with the table write-locked to 0.06 s, and the queue stays around one chunk instead of a million jobs (decision #15).

## Changing the prompt or the model

`bee.update_column(...)` inserts a new row in `bee.column_version` and makes it current. Existing results point to the version that produced them, so "stale" is not a flag to maintain: it is the difference between the current version and the result's version. The function starts a backfill that enqueues, chunk by chunk, every row whose result is missing or not current, skipping rows with a human value. Recomputation is incremental by construction.

## Spending cap

`budget_usd` in a definition's config (with `budget_period` `day`, `month` or `total`) limits what that column can spend. `bee.complete_job` adds `usage.cost` to `bee.spend` per UTC day for every model result it records; `bee.claim_jobs` evaluates the budget once per definition and skips the exhausted ones, whose jobs stay `pending` until the period renews or `bee.configure` raises the cap (and wakes the workers with a `NOTIFY`). The check happens at claim time, so the cap can be exceeded by at most one batch in flight per worker. Default: no cap (decision #13). `pgbee status` and the `bee.budgets` view show spend and remainder.

## Retention

`bee.result` keeps every value ever produced and `bee.job` every completed job: without cleanup both grow forever. `bee.prune` deletes, in batches, the non-current results older than their column's `lineage_retention_days` (default: keep everything) and `done` jobs older than a week. It never touches current results (the value behind each cell, model or human), `dead` jobs or `bee.spend`, so budgets stay exact; `bee.cost_by_column` only counts the results that remain. The reference worker calls it at start and then every `PGBEE_MAINTENANCE_INTERVAL_SECONDS` (default one hour), in batches of 10,000 while there is something to delete; a maintenance error is logged and does not stop the queue (decision #17).

## Human overrides

An `UPDATE ticket SET urgency = 'low'` outside the worker's guard fires the override trigger: it inserts a result with `source = 'human'`, makes it current and deletes the live jobs for that row. From then on the row is pinned: neither a new prompt nor a change of its sources recomputes it, unless the definition uses `until_source_change`. `bee.unpin(table, column, pk)` releases the pin and requeues the row; an `UPDATE` that sets the column to NULL does the same, the natural gesture for "recompute this". The set of human overrides is also the basis for future few-shot prompting (see ROADMAP).

## Confidence

With the `llm` backend the model self-reports its confidence in the JSON output; with `decision` it is the probability of the chosen class. Both end up in `bee.result.confidence` and feed `bee.needs_review` (rows below the definition's threshold). Policy `low_confidence_policy`: `write` (default, writes anyway) or `hold` (leaves the column NULL and keeps the value only in the lineage until a human confirms). In the field test both kinds of confidence separated doubtful rows (agreement 86-88% at 0.9 or more, 32-35% below 0.5), but neither is a calibrated probability of being right.

## Row identity

The extension requires a primary key on the target table and reads it from `pg_constraint` in `bee.add_column`. The key travels as `jsonb` (`{"id": 42}`), so bigint, uuid and composite keys go through the same code. All dynamic SQL uses `format('%I')` for identifiers and bound parameters for values.

## Backends

Each version of a definition declares a `backend`, that is who produces the value (decisions #10 and #12):

- `llm`: a language model with structured output. The worker builds the JSON schema from the declared type, sends prompt and sources, and gets value and confidence. One row per call, bounded concurrency.
- `decision`: a typed-answer model (TypeSafe Jev, through OpenRouter's `decisions` endpoint). No generation: the column becomes a question (a choice among described classes, yes/no, a rubric score) and the answer carries probabilities, stored in `bee.result.details`. Only `enum`, `boolean`, `integer`, `numeric`. In the field test it matched a small LLM on classification at a fifth of the cost per question and a sixth of the latency. `decision` columns of the same row with the same model and sources become questions of a single call (up to 16): the text is paid once, and three questions cost about half of three calls. To make this happen during a backfill too, where the jobs of different columns sit far apart in the queue, `bee.claim_jobs` brings along the ready `decision` jobs of the same rows and model (decision #14).
- `embedding`: an embedding model. The worker sends sources in batches (hundreds per call) and gets one vector per row, written to a pgvector `vector` column of the declared dimension. No confidence.
- `custom`: a worker written by the user (geocoding, OCR, an internal service) computes the value, consuming the jobs of its definitions through the same contract. The extension neither knows nor cares how.

The four backends share everything else: queue, hashes, versions, selective recomputation, lineage, overrides. Switching embedding model and redoing the vectors is an `update_column`, like changing a prompt.

## Output types

`enum` (allowed values, a `text` column validated in `complete_job`), `text`, `boolean`, `integer`, `numeric`, `jsonb` (validated by the worker against its JSON schema), `vector` (declared dimension, requires pgvector). A `vector` is one per row: chunking into several vectors per row is postponed (decision #11).

## Worker contract

The functions `bee.claim_jobs`, `bee.complete_job`, `bee.fail_job`, `bee.reclaim_stale`, `bee.prune` (maintenance) and the `bee_jobs` channel are the public interface. A worker in any language that honours this contract is a valid worker: whoever must go through an internal gateway writes their own. The Python worker is the reference implementation, not the only one.

## Key decisions

Decisions with their discarded alternatives are numbered in `DECISIONS.md` (Italian). The main ones: plain SQL instead of a compiled extension (#1), an external worker instead of calls from the database (#2), Python for the reference worker with Rust postponed (#3), staleness computed from versions instead of stored (#4), overrides pinned by default (#5), self-reported confidence declared as a heuristic (#6), psycopg without an ORM in the worker (#7), scope limited to derived columns, never a job system (#10), one embedding per row (#11), a `decision` backend separate from `llm` (#12), spending cap applied at claim (#13), `decision` siblings in the same claim (#14), chunked backfill driven by the claim (#15), least-privilege worker through definer functions (#16), per-column lineage retention run by the worker (#17), two install paths generated from the same files (#18), the name `pgbee` with schema `bee` (#19), one lane and one connection per backend in the worker (#20), Apache-2.0 (#21).

## Boundaries

- The extension makes no network calls and knows no provider or prompt template: it stores text and configuration and hands them to the worker.
- The worker does not know the user's schema: it only works with the tables and functions of the `bee` schema.
- The demo contains no logic of its own: it uses the extension and the worker as a user would. If the demo needs a shortcut, the product is missing something.
- User tables receive only the target column. State, versions, confidence and costs are read from the `bee` schema, never from auxiliary columns added to the table.

## Security and privileges

The install creates the `bee_worker` role (it needs `CREATEROLE`; otherwise a superuser creates it once). A worker logs in with a member of that role (`CREATE ROLE pgbee_worker LOGIN PASSWORD '...' IN ROLE bee_worker`) and gets only `USAGE` on the `bee` schema, `EXECUTE` on the contract functions and `SELECT` on the views: no access to user tables nor to the `bee` tables. The contract functions and the enqueue and override triggers are `SECURITY DEFINER` with `search_path = pg_catalog, pg_temp`, so they run as the extension owner; for the same reason an application role with only `INSERT` and `UPDATE` on a table can enqueue and record overrides without grants on the `bee` schema. `EXECUTE` on the contract is revoked from `PUBLIC`: a definer function open to everyone would let anyone read sources through `claim_jobs` or write target columns through `complete_job`. Management functions (`add_column` and the others) stay `SECURITY INVOKER` and need the table owner. Prompts are data, not code: they live in the catalog and are never interpolated into SQL (decision #16). The threat model for users is in `SECURITY.md`.

## Accepted tradeoffs

- Derived columns are eventually consistent, by design: between an insert and its value there is the time of a batch.
- The backfill only advances while a worker claims: without a worker the scan stays at its first chunk, visible in `bee.columns.backfill_pending`.
- Bulk loads: the trigger enqueues row by row, and inserting 1M rows with an active derived column takes 38 s against 3.7 s without. Most of that is writing a million jobs (10 s alone) and computing key and hash per row; a statement-level trigger with transition tables measured 33 s and was discarded, since it would also fire on updates that do not touch the sources. The recipe is `bee.disable`, load, `bee.enable`: 6.7 s for the load and 19 ms to re-enable, then the chunked backfill enqueues the rows.
- Right after a bulk load that enqueues many jobs, until autovacuum refreshes the statistics of `bee.job` (about a minute), the planner believes the queue is empty and a claim takes 1 to 3 s instead of a few milliseconds. The recipe above avoids it, because the queue stays around one chunk.
- The cost per row depends on the provider and is known only afterwards, in `bee.result.usage`: the spending cap (#13) stops a column at claim time, it does not estimate in advance what a backfill will cost.
