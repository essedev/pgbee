# pgbee

AI-derived columns for PostgreSQL. This package is the reference worker and installer:

- `pgbee install` applies the extension's SQL files to a database (works on managed Postgres too);
- `pgbee run` consumes the queue and calls the models through OpenRouter, one lane per backend;
- `pgbee status` shows each derived column, its queue and its spend;
- `pgbee extension-files DIR` writes the files for `CREATE EXTENSION pgbee` on self-hosted servers.

The SQL files ship inside the package. Documentation, quickstart and source:
https://github.com/essedev/pgbee

Licensed under Apache-2.0.
