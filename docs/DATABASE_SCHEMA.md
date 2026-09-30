# Database Schema

Schema `bee`, creato dall'estensione. Le tabelle dell'utente ricevono solo la colonna target.

## Convenzioni

- PostgreSQL 15+.
- PK `bigserial` sulle tabelle dell'estensione; le righe dell'utente sono identificate da `row_pk jsonb` (`{"id": 42}`, chiavi composte come oggetto con più chiavi).
- Timestamp `created_at`, `updated_at` con `timestamptz`. Soft delete con `deleted_at` solo su `column_def`: le altre tabelle sono log o coda.
- Hash delle sorgenti: SHA-256 dei valori delle colonne sorgente serializzati come `jsonb` ordinato, in `bytea`.
- Enum PostgreSQL creati in modo idempotente dallo script di install.

## Entità

### column_def

Una colonna derivata dichiarata. La configurazione che cambia il risultato (prompt, modello, schema di output) sta in `column_version`; qui sta ciò che identifica la colonna e le policy operative.

| Campo | Tipo | Note |
|---|---|---|
| id | bigserial | PK |
| table_schema | text | schema della tabella utente |
| table_name | text | |
| column_name | text | colonna target |
| pk_columns | text[] | letta da `pg_constraint` in `add_column` |
| source_columns | text[] | colonne che alimentano il modello |
| output_type | bee.output_type | `enum`, `text`, `boolean`, `integer`, `numeric`, `jsonb`, `vector`, `halfvec` |
| current_version_id | bigint | versione corrente in `column_version`, nullable solo durante la creazione. Senza FK: formerebbe un ciclo con `column_version.column_def_id` e romperebbe il restore dell'estensione (0010) |
| config | jsonb | `batch_size`, `max_attempts`, `backoff_base_seconds`, `confidence_threshold`, `low_confidence_policy` (`write`/`hold`), `override_policy` (`pin`/`until_source_change`), `concurrency`, `budget_usd` (numero in USD, null senza tetto), `budget_period` (`day`/`month`/`total`, default `month`), `backfill_chunk` (righe per chunk di backfill, 1-100000, default 1000), `lineage_retention_days` (intero >= 1, null per conservare tutto, default null), `input_usd_per_mtok` e `output_usd_per_mtok` (USD per milione di token, >= 0 o null: il worker li usa per il costo quando il provider non lo riporta). Validato da un trigger su insert e update |
| enabled | boolean | disabilitata: i trigger restano ma non accodano |
| backfill_pending | boolean | una scansione della tabella è in corso |
| backfill_cursor | jsonb | chiave primaria dell'ultima riga scansionata, NULL prima del primo chunk |
| backfill_scanned | bigint | righe scansionate dalla scansione in corso o dall'ultima |
| created_at, updated_at, deleted_at | timestamptz | |

Unique parziale su `(table_schema, table_name, column_name) WHERE deleted_at IS NULL`.

### column_version

Ogni configurazione che ha prodotto risultati. Non si modifica mai: un cambiamento crea una riga nuova.

| Campo | Tipo | Note |
|---|---|---|
| id | bigserial | PK |
| column_def_id | bigint | FK |
| version | integer | progressivo per definizione |
| backend | bee.backend | `llm`, `decision`, `embedding`, `custom` |
| prompt | text | istruzione per il modello, senza template; per `decision` sono le instructions della domanda; null per `embedding` e `custom` |
| model | text | id del modello presso il provider del worker, es. `anthropic/claude-haiku-4.5` su OpenRouter o `qwen3:0.6b` su Ollama; per `custom` un nome libero che identifica il worker |
| output_schema | jsonb | per `enum` la lista dei valori oppure un oggetto `{valore: descrizione}` (obbligatorio con `decision`, dove le descrizioni sono i criteri); per `boolean` con `decision` opzionale `{"true": ..., "false": ...}`; per `integer` e `numeric` con `decision` `{"levels": [2-10 descrizioni]}`; per `jsonb` il JSON schema; per `vector` e `halfvec` `{"dimensions": 1024}`; per gli altri vincoli opzionali (min, max, max_length) |
| backend_config | jsonb | parametri del backend: temperature per `llm`, batch size di chiamata per `embedding`, libero per `custom` |
| created_at | timestamptz | |

Unique su `(column_def_id, version)`.

### job

La coda. Al massimo un job vivo per riga e colonna.

| Campo | Tipo | Note |
|---|---|---|
| id | bigserial | PK |
| column_def_id | bigint | FK |
| row_pk | jsonb | |
| source_hash | bytea | hash delle sorgenti al momento dell'ultimo accodamento; il trigger lo aggiorna se la riga cambia mentre il job è vivo |
| status | bee.job_status | `pending`, `claimed`, `done`, `dead`. Un tentativo fallito ma ripetibile torna `pending` con `next_attempt_at` nel futuro |
| attempts | integer | incrementato al claim; `fail_job` confronta con `max_attempts` |
| next_attempt_at | timestamptz | il claim prende solo job con `next_attempt_at <= now()` |
| claimed_by | text | id del worker |
| claimed_at | timestamptz | base per `reclaim_stale` |
| claimed_version_id | bigint | versione corrente al momento del claim: quella con cui il worker calcola. `complete_job` registra il risultato sotto questa versione e, se nel frattempo ne è arrivata una nuova, lo tiene fuori dai correnti e riaccoda la riga (0011) |
| last_error | text | |
| created_at, updated_at | timestamptz | |

Unique parziale su `(column_def_id, row_pk) WHERE status IN ('pending', 'claimed')`. Indice parziale su `(next_attempt_at, id)` per i `pending`, su `claimed_at` per i `claimed`. I job `done` si potano con `bee.prune_jobs(interval)`. Se la riga sorgente sparisce prima del claim, il job viene cancellato.

### result

Il lineage: ogni valore mai prodotto per una riga e colonna, da modello o da umano.

| Campo | Tipo | Note |
|---|---|---|
| id | bigserial | PK |
| column_def_id | bigint | FK |
| column_version_id | bigint | FK, null per i risultati umani |
| row_pk | jsonb | |
| source_hash | bytea | sorgenti su cui il valore è stato calcolato |
| value | jsonb | il valore, anche quando la policy `hold` non lo scrive nella colonna |
| confidence | real | null per i risultati umani |
| source | bee.result_source | `model`, `human` |
| is_current | boolean | uno solo per riga e colonna |
| written | boolean | false se `hold` ha trattenuto il valore |
| model | text | modello che ha risposto davvero (può differire dal richiesto per fallback del provider) |
| usage | jsonb | token e costo: il costo è quello riportato dal provider (OpenRouter) o calcolato dal worker dai prezzi per token della colonna |
| latency_ms | integer | |
| details | jsonb | extra del backend: per `decision` le probabilità per classe (`probabilities`), la probabilità del vero (`probability_true`), il punteggio grezzo e la legenda, `questions_in_call` quando la chiamata era condivisa con altre colonne |
| created_at | timestamptz | |

Unique parziale su `(column_def_id, row_pk) WHERE is_current`. Indice su `(column_def_id, column_version_id)` per trovare le righe stale, e su `(column_def_id, created_at) WHERE NOT is_current` per la retention. `bee.prune` cancella le righe non correnti più vecchie di `lineage_retention_days`; le correnti restano sempre.

### spend

La spesa per colonna e giorno UTC, alimentata da `bee.complete_job` per ogni `result` da modello che registra (anche quelli scartati perché la riga era cambiata: sono stati pagati). Non da un trigger su `bee.result`, che scatterebbe anche durante un `pg_restore`. Resta anche quando il lineage verrà potato.

| Campo | Tipo | Note |
|---|---|---|
| column_def_id | bigint | FK, PK con `day` |
| day | date | giorno UTC del risultato |
| cost | numeric | somma di `usage.cost`, in USD |
| results | bigint | risultati da modello contati |

Non conta le chiamate fallite dopo essere state pagate (output fuori schema): la spesa reale può superare questa di poco.

### schema_version

| Campo | Tipo | Note |
|---|---|---|
| version | integer | PK, numero del file SQL applicato |
| applied_at | timestamptz | |

## Viste

- `bee.columns`: definizioni con versione corrente espansa, conteggi di job per stato e stato del backfill (`backfill_pending`, `backfill_scanned`).
- `bee.stale_rows`: righe il cui risultato corrente ha una versione diversa dalla corrente della definizione, escluse le umane.
- `bee.needs_review`: risultati correnti da modello con `confidence < confidence_threshold`.
- `bee.dead_jobs`: job esauriti con ultimo errore.
- `bee.cost_by_column`: somma di token e costo per definizione e versione.
- `bee.budgets`: per definizione il tetto, il periodo, lo speso nel periodo corrente e in totale, il residuo e `exhausted`.

## Funzioni pubbliche

Gestione: `bee.add_column`, `bee.update_column` (nuova versione), `bee.configure` (policy senza versione), `bee.drop_column`, `bee.enable`, `bee.disable`, `bee.backfill`, `bee.unpin`, `bee.retry_dead`, `bee.prune_jobs`, `bee.spent(def_id, period)`. Ogni funzione ha un `COMMENT` leggibile con `\df+ bee.*`.

Contratto worker: `bee.claim_jobs(worker_id, batch_size, backends[])`, `bee.complete_job(job_id, source_hash, value, confidence, model, usage, latency_ms, details)` che restituisce `bee.complete_outcome` (`written`, `held`, `stale_requeued`, `cancelled`), `bee.fail_job(job_id, error, retryable)` che restituisce lo stato risultante, `bee.reclaim_stale(timeout)`, e per la manutenzione `bee.prune(limit, jobs_older_than)` che restituisce `(results, jobs)` cancellati, ciascuno al massimo `limit`. `claim_jobs` fa avanzare i backfill in corso prima e dopo aver preso i job, salta le definizioni con budget esaurito e aggiunge ai job `decision` scelti i job `decision` pronti delle stesse righe e dello stesso modello, quindi può restituire più di `batch_size` righe. Canale `NOTIFY bee_jobs` con l'id della definizione a ogni accodamento e a ogni `bee.configure` (alzare un budget sveglia subito i worker).

Trigger per tabella utente: `bee_enqueue_<column>` (AFTER INSERT OR UPDATE OF sorgenti) e `bee_override_<column>` (AFTER UPDATE OF target). Il secondo ignora le scritture fatte dentro `complete_job` (GUC `bee.writer = 'worker'`); un UPDATE a NULL fatto a mano toglie il pin e riaccoda la riga.

## Ruoli e privilegi

`bee_worker` (NOLOGIN, creato dall'install): `USAGE` sullo schema `bee`, `EXECUTE` su `claim_jobs`, `complete_job`, `fail_job`, `reclaim_stale`, `prune` (revocato a `PUBLIC`), `SELECT` su `bee.columns`, `bee.budgets`, `bee.dead_jobs`, `bee.needs_review`, `bee.stale_rows`, `bee.cost_by_column`. Le funzioni del contratto, `bee.prune` e le funzioni trigger `bee.enqueue_trigger` e `bee.override_trigger` sono `SECURITY DEFINER` con `search_path = pg_catalog, pg_temp`.

## Relazioni

`column_def` 1-N `column_version`; `column_def` 1-N `job`; `column_def` 1-N `result`; `column_version` 1-N `result`; `column_def` 1-N `spend`. Le tabelle dell'utente non hanno FK verso lo schema `bee`: il legame è per `row_pk`, e la cancellazione di una riga utente lascia il lineage orfano di proposito (storia).

## Migrazioni

File numerati in `worker/src/pgbee/sql/` (`sql/` alla radice è un link), tracciati in `bee.schema_version`. Un file applicato non si riscrive. Due modi di applicarli, esclusivi fra loro:

- `pgbee install`: una transazione per file, funziona su qualunque Postgres raggiungibile, gestiti inclusi.
- `CREATE EXTENSION pgbee`: `pgbee extension-files` genera `pgbee.control`, `pgbee--0.1.sql` dal file 0001 e uno script `pgbee--0.(N-1)--0.N.sql` per ogni file successivo; Postgres li concatena sia all'installazione sia con `ALTER EXTENSION pgbee UPDATE`. Ogni script marca tabelle e sequenze di `bee` (tranne `schema_version`) con `pg_extension_config_dump`, così `pg_dump` ne salva i dati. Richiede accesso alla `sharedir` del server, quindi solo Postgres self-hosted.

`pgbee install` rifiuta un database dove l'estensione esiste già.
