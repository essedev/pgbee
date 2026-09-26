# pgbee

AI-derived columns for PostgreSQL. Like a worker bee filling cells: you declare how a column is derived, pgbee keeps every row filled, versioned and accounted for.

Declare once that `ticket.urgency` is derived from `ticket.body` with a prompt and a model. From then on every new or changed row gets its value: transactional queue, batching, retries, prompt and model versioning, per-row lineage, confidence, human overrides that stick, and a spending cap per column. The database owns the guarantee; an external worker makes the model calls. The model is one backend, not the product.

Status: experiment. See `docs/ANALYSIS.md` for the reasoning and the exit criterion.

## Stack

- Extension: plain SQL and PL/pgSQL in `sql/`, installs on any PostgreSQL 15+ including managed ones, or as a real extension (`CREATE EXTENSION pgbee`) on self-hosted servers.
- Worker: Python 3.13 package `pgbee` in `worker/`, psycopg 3, OpenRouter via the OpenAI SDK.
- Demo: Docker Compose Postgres plus a support-ticket dataset in `demo/`, and a field test on 3000 real CFPB consumer complaints in `demo/cfpb/`.

## Quick start

```bash
make install        # uv sync
make db-up          # Postgres on port 4460
make db-install     # apply sql/ to the database
make demo           # seed tickets, declare derived columns, run the worker
```

Outside this repository:

```bash
make build                                   # worker/dist/pgbee-*.whl, SQL files included
uvx --from worker/dist/pgbee-0.1.0-py3-none-any.whl pgbee install   # needs DATABASE_URL
# or, on a self-hosted server, as a Postgres extension:
pgbee extension-files ./ext && cp ./ext/* "$(pg_config --sharedir)/extension/"
psql -c 'CREATE EXTENSION pgbee'
```

`make worker-image` builds `pgbee-worker:dev` (`docker run --env-file ... pgbee-worker:dev` runs the queue; `.env` files never enter the image). `make extension-image` builds `pgbee-postgres:dev`, pgvector's image with the extension files in place.

Configuration is read from `worker/.env` (see `worker/.env.example`). In production the worker should log in with a role in `bee_worker`, which only gets the queue functions: `CREATE ROLE pgbee LOGIN PASSWORD '...' IN ROLE bee_worker`.

## Documentation

- [Analysis and competition](docs/ANALYSIS.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Database schema](docs/DATABASE_SCHEMA.md)
- [Decisions](docs/DECISIONS.md)
- [Cycles](docs/CYCLES.md)
- [Conventions](docs/CONVENTIONS.md)
- [Roadmap](docs/ROADMAP.md)
