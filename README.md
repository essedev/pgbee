<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/brand/pgbee-logo-dark.svg">
    <img alt="pgbee" src="assets/brand/pgbee-logo.svg" width="340">
  </picture>
</p>

<p align="center">AI-derived columns for PostgreSQL</p>

Like a worker bee filling cells: you declare how a column is derived from other columns, and pgbee keeps every row filled, versioned and accounted for.

```sql
SELECT bee.add_column('ticket', 'category', array['body'], 'enum', 'decision',
  p_prompt => 'What is this support ticket about?',
  p_model  => 'typesafe/jev-1.13',
  p_output_schema => '{"bug": "something is broken", "billing": "charges and invoices",
                       "account": "login and account data", "question": "how to do something"}');
```

From then on every new or changed row gets its `category`: the database queues the work in the same transaction as your insert, an external worker calls the model, and the value lands in the column. Change the prompt and only the rows computed with the old one are recomputed. Correct a value by hand and the model never overwrites it. Every value keeps its lineage: which model, which prompt version, which confidence, how much it cost.

Calling a model from SQL is easy and many tools do it. The hard part, and the point of pgbee, is the state around the call: what is stale, what was overridden, what failed, what it costs. [Under failure](#under-failure) shows what goes wrong when that state lives in application code instead.

**Status: alpha (0.1).** The test suite passes on PostgreSQL 15, 16, 17 and 18. A field test on 3000 real consumer complaints ran 12,000 model calls with no failure, and a failure test with crashes, outages and concurrent edits ended with no wrong value (numbers below). The whole suite and the worker also ran on a managed service, Neon (PostgreSQL 18, no superuser). Nobody runs it in production yet. Feedback and issues are welcome.

## Quickstart

You need Docker and an [OpenRouter](https://openrouter.ai) key: the example uses a decision model that is only available there. The worker also runs on OpenAI, Azure or a local model, see [Model providers](#model-providers).

```bash
git clone https://github.com/essedev/pgbee && cd pgbee
echo 'OPENROUTER_API_KEY=sk-or-...' > .env
docker compose up -d                                   # Postgres, pgbee install, worker
docker compose exec -T postgres psql -U pgbee < examples/quickstart.sql
```

[`examples/quickstart.sql`](examples/quickstart.sql) creates a table of eight support tickets and declares three derived columns: a category (decision model), a one-line summary (LLM) and an embedding. A few seconds later:

```
docker compose exec postgres psql -U pgbee -c 'SELECT id, category, summary FROM ticket'

 id | category |                           summary
----+----------+-------------------------------------------------------------
  1 | bug      | Checkout error 500 prevents payments, causing lost orders.
  2 | billing  | Request a refund for a duplicate Pro plan charge.
  3 | question | Asking how to export a customer list to CSV
  4 | account  | Password reset email not received, preventing login.
  ...
```

The whole run costs about 0.0005 USD. Postgres listens on port 5432; set `PGBEE_PORT` to change it.

## How it works

```
INSERT/UPDATE ──trigger──▶ bee.job (queue, same transaction)
                               │  NOTIFY bee_jobs
                               ▼
                 worker: bee.claim_jobs ──▶ model (OpenRouter, OpenAI, local)
                               │
                               ▼
      bee.complete_job ──▶ target column + bee.result (lineage) + bee.spend
```

- **The database owns the guarantees.** Queue, versions, lineage, overrides and budgets live in the `bee` schema, written in plain SQL and PL/pgSQL: no compiled code, nothing to install on the server besides SQL. The worker never touches your tables directly: it calls four queue functions and one maintenance function.
- **The worker is replaceable.** The Python worker in this repository is the reference implementation. Any process that speaks the four-function contract (`claim_jobs`, `complete_job`, `fail_job`, `reclaim_stale`) is a valid worker.
- **Nothing is lost.** A row that changes while its value is being computed is queued again, and so is a row whose prompt changes mid-flight. A worker that dies leaves its jobs to be reclaimed. Failures back off exponentially and end in `bee.dead_jobs`.

## Backends and output types

| Backend | What it does | Models |
|---|---|---|
| `llm` | Structured output from a language model, with self-reported confidence | any chat model with JSON schema output, e.g. `openai/gpt-6-luna` on OpenRouter |
| `decision` | Typed questions (choice, yes/no, rubric score) to a decision model, with class probabilities | `typesafe/jev-1.13`, OpenRouter only |
| `embedding` | One vector per row, batched | e.g. `openai/text-embedding-3-small` |
| `custom` | Your own worker claims these jobs (`claim_jobs(..., array['custom'])`) | anything |

Output types: `enum`, `text`, `boolean`, `integer`, `numeric`, `jsonb`, `vector` (needs pgvector). The `decision` backend covers `enum`, `boolean`, `integer` and `numeric`. Several `decision` columns on the same row share one model call.

## Field test

3000 real complaints from the public [CFPB Consumer Complaint Database](https://www.consumerfinance.gov/data-research/consumer-complaints/), nine products, four derived columns, the real worker process ([`demo/cfpb/`](demo/cfpb/)):

| Column | Backend | Cost per 1000 rows | Median latency |
|---|---|---|---|
| product | `decision` (Jev) | 0.019 USD | 0.31 s |
| lost money? | `decision`, same call | 0.019 USD | 0.31 s |
| product | `llm` (gpt-6-luna, low effort) | 0.093 USD | 1.87 s |
| embedding | `embedding` | 0.005 USD | 0.03 s |

12,000 jobs, 0 failures, 0.41 USD in total. Both models agree with the product the consumer picked about 79% of the time. That label is noisy, so this measures agreement, not accuracy. When the models are confident (0.9 or more) the agreement is 86-88%; below 0.5 it drops to 32-35%. That is what the review queue is for.

## Under failure

Why keep this state in the database instead of in application code? [`bench/failure/`](bench/failure/) runs the same workload through pgbee and two app-side designs, with a fake model and no cost: save the row then enqueue on an external queue, and a transactional outbox. The queue is durable, with leases and retries. Processes are killed with SIGKILL about every 60 operations, the external queue goes down twice, the prompt changes halfway, a script edits rows behind the application's back, and people correct about 3% of the values. On 2000 rows, over three seeds:

| | pgbee | save, then enqueue | outbox |
|---|---|---|---|
| Rows left without a value | 0 | 1-4 | 0 |
| Stale values that look fresh | 0 | 30-49 | 15-44 |
| Human corrections overwritten (of 56-60) | 0 | 20-27 | 21-30 |

The outbox fixes lost rows and nothing else: stale values come from rows edited while computed, edits that bypass the application and jobs that read the old prompt, and corrections are lost because nothing records that a value came from a person. Details, fairness choices and the bug this test found in pgbee (fixed) are in [`bench/README.md`](bench/README.md).

## Reference

### Declaring and managing columns

| Function | What it does |
|---|---|
| `bee.add_column(p_table, p_column, p_source_columns, p_output_type, p_backend => 'llm', p_prompt, p_model, p_output_schema, p_backend_config, p_config)` | Declares a derived column: adds the target column if missing, installs the triggers, starts the backfill. |
| `bee.update_column(p_table, p_column, p_prompt, p_model, p_output_schema, p_backend_config)` | New version of the definition. Only rows computed with an older version are recomputed. |
| `bee.configure(p_table, p_column, p_config)` | Changes operational settings (below) without creating a version. |
| `bee.disable(p_table, p_column)` / `bee.enable(p_table, p_column)` | Pause and resume. `enable` recomputes the rows that changed meanwhile. |
| `bee.drop_column(p_table, p_column, p_drop_target => false)` | Stops deriving the column. Keeps the lineage; drops the column only when asked. |
| `bee.unpin(p_table, p_column, '{"id": 42}')` | Releases a human override and recomputes the row. Setting the column to NULL by hand does the same. |
| `bee.retry_dead(p_table, p_column)` | Puts failed jobs back in the queue. |
| `bee.spent(p_def_id, p_period)` | USD spent by a column in the current day, month, or in total. |

Parameters are named with a `p_` prefix, so named notation reads `p_prompt => '...'`. `p_output_schema`: for `enum` an array of values or an object `{"value": "description"}` (descriptions are required by `decision`); for `vector` `{"dimensions": N}`; for `decision` scores `{"levels": [...]}`. `p_backend_config` is passed to the provider: `{"reasoning": {"effort": "low"}}`, `temperature`, `max_tokens`, `dimensions`, `batch_size` for embeddings.

### Settings (`p_config`, per column)

| Key | Default | Meaning |
|---|---|---|
| `budget_usd` | none | Spending cap. When reached, the column's jobs wait until the period renews or the cap is raised. |
| `budget_period` | `month` | `day`, `month` or `total`. |
| `confidence_threshold` | 0.7 | Below this, a value goes to `bee.needs_review`. |
| `low_confidence_policy` | `write` | `write` the value anyway, or `hold` it back (column stays NULL, value kept in lineage). |
| `override_policy` | `pin` | A human value is never overwritten; `until_source_change` recomputes when the source changes. |
| `batch_size`, `concurrency` | 20, 4 | Jobs per claim and parallel model calls. |
| `max_attempts`, `backoff_base_seconds` | 5, 30 | Retries with exponential backoff. |
| `backfill_chunk` | 1000 | Rows enqueued per backfill step. Declaring a column on a large table never locks it for long. |
| `lineage_retention_days` | none | Superseded lineage older than this is pruned by the worker. Current values are never pruned. |
| `input_usd_per_mtok`, `output_usd_per_mtok` | none | Token prices in USD per million, for providers that report no cost (OpenAI, Azure, local models). The worker prices each call with them, so spend and budgets work. A cost reported by the provider wins. |

### Views

| View | Shows |
|---|---|
| `bee.columns` | Each derived column with its version, queue counters and backfill progress. |
| `bee.needs_review` | Current values below the confidence threshold. |
| `bee.stale_rows` | Rows computed with an older version. |
| `bee.dead_jobs` | Failed jobs with their last error. |
| `bee.budgets` | Cap, spend in the period and in total, remaining, exhausted. |
| `bee.cost_by_column` | Tokens, cost and latency per column and version. |

### Worker

```bash
pgbee install              # apply the SQL files (idempotent)
pgbee run                  # one lane per backend: fast models never wait for slow ones
pgbee run --backends llm   # serve only some backends (scale them on separate machines)
pgbee run --drain          # process until nothing is ready, then exit (cron, serverless)
pgbee status               # columns, queues, spend
pgbee extension-files DIR  # files for CREATE EXTENSION pgbee
```

Environment: `DATABASE_URL`, the provider variables below, and optionally `PGBEE_WORKER_ID`, `PGBEE_POLL_INTERVAL_SECONDS`, `PGBEE_CLAIM_TIMEOUT_SECONDS`, `PGBEE_REQUEST_TIMEOUT_SECONDS` (per model call, default 60), `PGBEE_MAINTENANCE_INTERVAL_SECONDS`, `PGBEE_LOG_LEVEL`. Releases publish the worker as a Python package (`uvx pgbee`) and as Docker images on GHCR; from a clone, `make build` and `make worker-image` build them locally.

### Without a long-running worker

Where no process can stay up (serverless platforms, a shared host), run the worker from a scheduler: `pgbee run --drain` processes batches until nothing is ready, then exits.

```bash
# crontab: every minute, never longer than 50 seconds
* * * * *  DATABASE_URL=... OPENROUTER_API_KEY=... pgbee run --drain --max-seconds 50
```

The same command works as a Kubernetes CronJob, a scheduled Cloud Run job or GitHub Actions workflow, with the worker image. Each run first gives back the jobs of runs that died (after `PGBEE_CLAIM_TIMEOUT_SECONDS`, which must stay above `--max-seconds` plus one batch) and does the maintenance; runs that overlap are safe, they claim different jobs. The price is latency: a value appears at the next run, and a large backfill advances only while a run is active.

### Model providers

The worker talks to one provider, picked from the environment:

| Provider | Variables | Backends |
|---|---|---|
| [OpenRouter](https://openrouter.ai) (default) | `OPENROUTER_API_KEY`, optional `OPENROUTER_BASE_URL` | `llm`, `decision`, `embedding` |
| Any OpenAI-compatible endpoint: OpenAI, Azure OpenAI (v1 endpoint), Ollama, vLLM | `OPENAI_API_KEY` and/or `OPENAI_BASE_URL` (default `https://api.openai.com/v1`) | `llm`, `embedding` |

With both keys set OpenRouter wins; `PGBEE_PROVIDER=openai` or `openrouter` forces the choice. For a local model, `OPENAI_BASE_URL=http://localhost:11434/v1` (Ollama) is enough, no key needed. OpenRouter reports the cost of each call; the others report only tokens, so declare the prices in the column (`input_usd_per_mtok`, `output_usd_per_mtok`) if you want spend and budgets. With an OpenAI-compatible endpoint the worker sends only standard parameters from `p_backend_config` (`temperature`, `top_p`, `seed`, `max_tokens`, `max_completion_tokens`, `reasoning_effort`; `{"reasoning": {"effort": ...}}` is translated). The model id is the provider's own (`gpt-6-luna` on OpenAI, `qwen2.5:0.5b` on Ollama), and `llm` models need JSON schema output. A local server usually runs one request at a time: lower the column's `concurrency` and raise `PGBEE_REQUEST_TIMEOUT_SECONDS` so queued calls do not time out. To use both OpenRouter and a local model, run two workers with different `--backends` (for example `decision` on OpenRouter, `llm,embedding` local): a worker serves every column of the backends it claims, so two columns of the same backend cannot go to different providers.

## Installing on your database

**Any Postgres 15+, managed ones included:** run `pgbee install` with a `DATABASE_URL` whose role can create schemas and roles (`CREATEROLE`). It creates the `bee` schema and the `bee_worker` role. Without `CREATEROLE`, a superuser creates `bee_worker` once (`CREATE ROLE bee_worker NOLOGIN`) and the install goes on. pgvector is needed only for `vector` columns. Tested on Neon with the owner role the console creates.

**Use a direct connection for the worker, not a transaction pooler.** The worker wakes up on `LISTEN bee_jobs`, which needs a session: through a transaction pooler (PgBouncer, Neon's `-pooler` host, Supabase's pooler on port 6543) the notifications are lost and the worker only finds new jobs at its poll interval (`PGBEE_POLL_INTERVAL_SECONDS`, default 5). Everything else works there. On Neon, from insert to written value: 0.3 s with the direct host, 3.9 s through the pooler. Your application can keep using the pooler.

**Self-hosted, as a real extension:** `pgbee extension-files ./ext`, copy the files into `$(pg_config --sharedir)/extension/`, then `CREATE EXTENSION pgbee`. Upgrades with `ALTER EXTENSION pgbee UPDATE`, and `pg_dump` keeps the definitions, lineage and spend. The two ways are generated from the same SQL files and are mutually exclusive on a database.

**Give the worker its own login:** `CREATE ROLE pgbee_worker LOGIN PASSWORD '...' IN ROLE bee_worker`. That role can only call the queue functions and read the views. It has no access to your tables, because the contract functions run as the extension owner.

**Bulk loads:** the triggers enqueue row by row. For a load of millions of rows, `bee.disable` the column, load, then `bee.enable`: the backfill enqueues the rows in chunks.

## Limits and security

- **Your data goes to the model provider.** Source column values are sent to the provider the worker uses (OpenRouter and the model behind it, OpenAI, Azure). Do not derive columns from data you may not share with them, or use a local model (Ollama, vLLM), where the data stays on your machines.
- **Prompt injection.** Row text is model input. A row can contain instructions that bias its own value. Values are validated against the declared type (an enum stays an enum), but not against intent. Jev's documentation lists this as a known weakness.
- **Confidence is a signal, not a guarantee.** It separates doubtful rows well in our tests, but it is not calibrated for LLMs.
- **Budgets are checked when jobs are claimed**, so a cap can be exceeded by one batch in flight. Calls that were paid for but returned an invalid answer are not counted.
- **One provider per worker process:** OpenRouter or one OpenAI-compatible endpoint (see [Model providers](#model-providers)). `decision` uses OpenRouter's decisions API, still marked alpha. Anything else means a provider class in the worker or your own worker: the SQL contract does not depend on any provider.
- **Writing the value a cell already holds is not an override.** ORMs often rewrite every column on save, so only a change counts as a human correction.

To report a vulnerability, see [SECURITY.md](SECURITY.md).

## Development

```bash
make install      # worker dependencies (uv)
make db-up        # Postgres for demo and tests on port 4460
make check        # format, lint, typecheck, tests
make test-llm     # also the tests that call OpenRouter (costs a fraction of a cent)
make test-extension
DATABASE_URL=postgresql://... make test   # the suite on another server; creates and drops a database aidb_test
make demo         # the Italian support-ticket demo, end to end
make bench-failure  # the failure test above; bench-scale and bench-lanes too (bench/README.md)
```

Changes between releases: [CHANGELOG.md](CHANGELOG.md). Design notes: [architecture](docs/ARCHITECTURE.md) (English). The working notes are in Italian: [analysis and competition](docs/ANALYSIS.md), [decisions](docs/DECISIONS.md), [database schema](docs/DATABASE_SCHEMA.md), [cycles](docs/CYCLES.md), [roadmap](docs/ROADMAP.md), [conventions](docs/CONVENTIONS.md).

## License

[Apache-2.0](LICENSE). Made by [Simone Salerno](https://github.com/essedev).
