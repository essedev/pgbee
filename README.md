# ai-db

Derived columns for PostgreSQL, computed by a model and maintained by the database.

Declare once that `ticket.urgency` is derived from `ticket.body` with a prompt and a model. From then on every new or changed row gets its value: transactional queue, batching, retries, prompt and model versioning, per-row lineage, confidence, human overrides that stick, and a spending cap per column. The database owns the guarantee; an external worker makes the model calls. The model is one backend, not the product.

Status: experiment. See `docs/ANALYSIS.md` for the reasoning and the exit criterion.

## Stack

- Extension: plain SQL and PL/pgSQL in `sql/`, installs on any PostgreSQL 15+ including managed ones.
- Worker: Python 3.13 package `aicol` in `worker/`, psycopg 3, OpenRouter via the OpenAI SDK.
- Demo: Docker Compose Postgres plus a support-ticket dataset in `demo/`.

## Quick start

```bash
make install        # uv sync
make db-up          # Postgres on port 4460
make db-install     # apply sql/ to the database
make demo           # seed tickets, declare derived columns, run the worker
```

Configuration is read from `worker/.env` (see `worker/.env.example`).

## Documentation

- [Analysis and competition](docs/ANALYSIS.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Database schema](docs/DATABASE_SCHEMA.md)
- [Decisions](docs/DECISIONS.md)
- [Cycles](docs/CYCLES.md)
- [Conventions](docs/CONVENTIONS.md)
- [Roadmap](docs/ROADMAP.md)
