# Changelog

All notable changes to pgbee: the SQL extension, the worker contract and the reference worker (`pgbee` on PyPI, images on GHCR). The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and versions follow [Semantic Versioning](https://semver.org/); before 1.0 a minor version may change the worker contract, and the release notes say so under Upgrading.

The package version (`0.1.0`) and the extension version (`0.N`, one per SQL file in `sql/`) are numbered separately. Each release lists the extension version it ships.

## [Unreleased]

## [0.1.0] - 2026-09-27

First public release.

### Added

- Derived columns declared in SQL: `bee.add_column` sets a prompt, a model, the source columns and an output type (`enum`, `text`, `boolean`, `integer`, `numeric`, `jsonb`, `vector`). Inserts and updates of the sources enqueue a job in the same transaction; the value is written back by a worker.
- Backends: `llm` (structured output with self-reported confidence), `decision` (typed questions to TypeSafe Jev with class probabilities; the `decision` columns of a row share one call, up to 16 questions), `embedding` (batched vectors, needs pgvector) and `custom` (jobs claimed by your own worker).
- Versions and lineage: `bee.update_column` creates a new version and recomputes only the rows computed with an older one. Every value is kept in `bee.result` with its model, prompt version, confidence, tokens, cost and latency; a value computed while the row or the version changed is recorded under the version it was computed with and the row is queued again.
- Human overrides: a value written by hand is never overwritten (`override_policy` `pin`, or `until_source_change`); `bee.unpin` or setting the cell to NULL gives it back to the model. Writing the value a cell already holds is not an override.
- Review queue: values below `confidence_threshold` are listed in `bee.needs_review`; `low_confidence_policy` `hold` keeps them out of the column.
- Budgets per column (`budget_usd` per day, month or in total), enforced when jobs are claimed, with `bee.budgets`, `bee.spent` and `bee.cost_by_column`. Token prices per column (`input_usd_per_mtok`, `output_usd_per_mtok`) for providers that report no cost.
- Retries with exponential backoff, dead jobs in `bee.dead_jobs` and `bee.retry_dead`, abandoned jobs reclaimed after a timeout.
- Backfill in chunks: declaring a column on a table of a million rows takes a fraction of a second and does not block writers. `bee.disable` and `bee.enable` for bulk loads.
- Lineage retention per column (`lineage_retention_days`), pruned in batches by `bee.prune`.
- Least-privilege worker: the `bee_worker` role can only call the contract functions and read the views; it has no access to user tables.
- Worker contract: `bee.claim_jobs`, `bee.complete_job`, `bee.fail_job`, `bee.reclaim_stale`, `bee.prune`, and the `bee_jobs` NOTIFY channel. Any process that speaks it is a valid worker.
- Two ways to install, generated from the same SQL files: `pgbee install` on any PostgreSQL 15 to 18, managed services included, or `CREATE EXTENSION pgbee` from `pgbee extension-files` on self-hosted servers, with `ALTER EXTENSION pgbee UPDATE` and `pg_dump` support.
- Reference worker `pgbee` (Python 3.13): `pgbee run` with one lane per backend so fast models never wait for slow ones, `--backends` to split backends across machines, `--drain` and `--max-seconds` for cron and serverless schedulers, `pgbee status`, `pgbee install`, `pgbee extension-files`.
- Model providers: OpenRouter (all backends, reported cost) or any OpenAI-compatible endpoint such as OpenAI, Azure OpenAI, Ollama or vLLM (`llm` and `embedding`, standard parameters only), chosen with `OPENROUTER_API_KEY`, `OPENAI_API_KEY`, `OPENAI_BASE_URL` or `PGBEE_PROVIDER`.
- Docker images: `ghcr.io/essedev/pgbee-worker` and `ghcr.io/essedev/pgbee-postgres` (PostgreSQL 17 and 18 with the extension available), and a `docker compose` quickstart.
- Benchmarks anyone can rerun without cost in `bench/`: a failure test against two app-side designs, scale on a million rows, lanes against a single loop.

### Upgrading

- Nothing to upgrade from. This release ships extension version 0.14 (SQL files 0001 to 0014). Install with `pgbee install`, or with `CREATE EXTENSION pgbee` after copying the files from `pgbee extension-files` (also attached to the GitHub release).
- On managed services (tested on Neon) connect the worker directly, not through a transaction pooler: the pooler drops the notifications that wake the worker, which then only polls every `PGBEE_POLL_INTERVAL_SECONDS`.

[Unreleased]: https://github.com/essedev/pgbee/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/essedev/pgbee/releases/tag/v0.1.0
