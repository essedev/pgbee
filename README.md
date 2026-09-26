# ai-db

Derived columns for PostgreSQL, computed by a model and maintained by the database.

Declare once that `ticket.urgency` is derived from `ticket.body` with a prompt and a model. From then on every new or changed row gets its value: transactional queue, batching, retries, prompt and model versioning, per-row lineage, confidence, human overrides that stick, and a spending cap per column. The database owns the guarantee; an external worker makes the model calls. The model is one backend, not the product.

Status: experiment. See `docs/ANALYSIS.md` for the reasoning and the exit criterion.

## Stack

- Extension: plain SQL and PL/pgSQL in `sql/`, installs on any PostgreSQL 15+ including managed ones, or as a real extension (`CREATE EXTENSION aicol`) on self-hosted servers.
- Worker: Python 3.13 package `aicol` in `worker/`, psycopg 3, OpenRouter via the OpenAI SDK.
- Demo: Docker Compose Postgres plus a support-ticket dataset in `demo/`.

## Quick start

```bash
make install        # uv sync
make db-up          # Postgres on port 4460
make db-install     # apply sql/ to the database
make demo           # seed tickets, declare derived columns, run the worker
```

Outside this repository:

```bash
make build                                   # worker/dist/aicol-*.whl, SQL files included
uvx --from worker/dist/aicol-0.1.0-py3-none-any.whl aicol install   # needs DATABASE_URL
# or, on a self-hosted server, as a Postgres extension:
aicol extension-files ./ext && cp ./ext/* "$(pg_config --sharedir)/extension/"
psql -c 'CREATE EXTENSION aicol'
```

`make extension-image` builds `aicol-postgres:dev`, pgvector's image with the extension files in place.

Configuration is read from `worker/.env` (see `worker/.env.example`). In production the worker should log in with a role in `ai_worker`, which only gets the queue functions: `CREATE ROLE aicol LOGIN PASSWORD '...' IN ROLE ai_worker`.

## Documentation

- [Analysis and competition](docs/ANALYSIS.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Database schema](docs/DATABASE_SCHEMA.md)
- [Decisions](docs/DECISIONS.md)
- [Cycles](docs/CYCLES.md)
- [Conventions](docs/CONVENTIONS.md)
- [Roadmap](docs/ROADMAP.md)
