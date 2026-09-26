# pgbee - Istruzioni per Claude

## Cosa è

Colonne derivate da modello per PostgreSQL: estensione in SQL puro (`sql/`) che possiede catalogo, coda, versioni, lineage e override; worker Python (`worker/`, pacchetto `pgbee`) che consuma la coda e chiama i modelli via OpenRouter; demo su ticket di assistenza (`demo/`). Il perché e i confini stanno in `docs/ARCHITECTURE.md`, il modello dati in `docs/DATABASE_SCHEMA.md`, le decisioni numerate in `docs/DECISIONS.md`.

## Stack

- Estensione: SQL e PL/pgSQL, Postgres 15+, nessun codice compilato. Schema `bee`.
- Worker: Python 3.13 con `uv`, psycopg 3 async (niente ORM, niente Alembic: le migrazioni sono i file di `sql/`), SDK OpenAI puntato a OpenRouter, typer per la CLI, structlog.
- Test: pytest su Postgres vero in Docker.
- Porte (portsage): Postgres 4460, demo API 4461, demo web 4462, container usa e getta di `make test-extension` 4463.

## Comandi (via Makefile)

- `make install`: dipendenze del worker.
- `make db-up` / `make db-down`: Postgres della demo e dei test in Docker.
- `make db-install`: applica `sql/` al database (`pgbee install`).
- `make worker`: avvia il worker (`pgbee run`).
- `make demo`: seed della demo, dichiarazione delle colonne, worker.
- `make status`: colonne derivate e contatori della coda (`pgbee status`).
- `make test`, `make test-llm` (include i test marcati `llm`, costa), `make lint`, `make format`, `make typecheck`, `make check`, `make build`, `make worker-image`, `make extension-image`, `make test-extension` (serve Docker, marker `extension`), `make clean`; `make help` li elenca tutti.
- Un solo run di test alla volta: il conftest ricrea il database.

## Convenzioni

Vedi `docs/CONVENTIONS.md`. Le regole per i file SQL sono in `.claude/rules/sql-extension.md` e si caricano da sole.

## Gotcha

- Il worker non deve mai contenere SQL su tabelle dell'utente: passa solo dalle funzioni e viste dello schema `bee`. Se serve leggere una tabella utente, la funzione va aggiunta all'estensione.
- Le funzioni del contratto worker (`bee.claim_jobs`, `bee.complete_job`, `bee.fail_job`, `bee.reclaim_stale`, `bee.prune`) sono interfaccia pubblica: cambiarne la firma è una decisione, non un refactor.
- Un file in `sql/` già committato non si modifica: correzione in un file nuovo. I file stanno in `worker/src/pgbee/sql/` (finiscono nel wheel) e `sql` alla radice è un link simbolico.
- Il worker scrive le colonne target solo tramite `bee.complete_job`, che imposta `bee.writer` con `SET LOCAL`: un UPDATE diretto viene letto dal trigger come override umano.
- Id dei modelli OpenRouter con il punto (`anthropic/claude-haiku-4.5`, `openai/gpt-6-luna`), non con il trattino. Verificare su openrouter.ai/models prima di proporne uno nuovo; per i benchmark lo snapshot pubblico di Artificial Analysis è `github.com/garo-pro/aa-leaderboards`, il confronto sui ticket è `demo/compare_models.py`.
- Jev (TypeSafe) si chiama via OpenRouter su `POST /api/alpha/decisions` con `typesafe/jev-1.13`, non su chat/completions; `typesafe/jev-router` è un router generico verso LLM, non Jev. Il backend `decision` richiede criteri con descrizioni nell'`output_schema`.
- Alcuni modelli rifiutano `reasoning.enabled = false` con un 400 (GLM 5.3 Flash): usare `effort: low`. I job finiscono `dead` correttamente, non è un bug del worker.
- `test_roles.py` crea ruoli di cluster (`pgbee_test_worker`, `pgbee_test_app`) e li droppa a fine modulo; `bee_worker` resta nel cluster, è creato dall'install.
- Test che chiamano un modello vero portano il marker `llm`, escluso dal giro di default.
- Costi: ogni batch verso OpenRouter costa. `demo/run.py` stampa la stima e chiede conferma; con `--yes` non chiede. Prima di lanciarla su più di qualche decina di righe, dichiarare la stima.
- L'SDK `openai` 3.x usa il pacchetto `httpx2`, non `httpx`: nei test le eccezioni si costruiscono con `httpx2.Request` e `httpx2.Response`.
- La demo si lancia dalla cartella `worker` (`make demo`) perché importa il pacchetto `pgbee`.
