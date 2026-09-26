# Database Schema

Schema `ai`, creato dall'estensione. Le tabelle dell'utente ricevono solo la colonna target.

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
| output_type | ai.output_type | `enum`, `text`, `boolean`, `integer`, `numeric`, `jsonb`, `vector` |
| current_version_id | bigint | versione corrente in `column_version`, nullable solo durante la creazione. Senza FK: formerebbe un ciclo con `column_version.column_def_id` e romperebbe il restore dell'estensione (0010) |
| config | jsonb | `batch_size`, `max_attempts`, `backoff_base_seconds`, `confidence_threshold`, `low_confidence_policy` (`write`/`hold`), `override_policy` (`pin`/`until_source_change`), `concurrency`, `budget_usd` (numero in USD, null senza tetto), `budget_period` (`day`/`month`/`total`, default `month`), `backfill_chunk` (righe per chunk di backfill, 1-100000, default 1000), `lineage_retention_days` (intero >= 1, null per conservare tutto, default null). Validato da un trigger su insert e update |
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
| backend | ai.backend | `llm`, `decision`, `embedding`, `custom` |
| prompt | text | istruzione per il modello, senza template; per `decision` sono le instructions della domanda; null per `embedding` e `custom` |
| model | text | id OpenRouter, es. `anthropic/claude-haiku-4.5`; per `custom` un nome libero che identifica il worker |
| output_schema | jsonb | per `enum` la lista dei valori oppure un oggetto `{valore: descrizione}` (obbligatorio con `decision`, dove le descrizioni sono i criteri); per `boolean` con `decision` opzionale `{"true": ..., "false": ...}`; per `integer` e `numeric` con `decision` `{"levels": [2-10 descrizioni]}`; per `jsonb` il JSON schema; per `vector` `{"dimensions": 1024}`; per gli altri vincoli opzionali (min, max, max_length) |
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
| status | ai.job_status | `pending`, `claimed`, `done`, `dead`. Un tentativo fallito ma ripetibile torna `pending` con `next_attempt_at` nel futuro |
| attempts | integer | incrementato al claim; `fail_job` confronta con `max_attempts` |
| next_attempt_at | timestamptz | il claim prende solo job con `next_attempt_at <= now()` |
| claimed_by | text | id del worker |
| claimed_at | timestamptz | base per `reclaim_stale` |
| last_error | text | |
| created_at, updated_at | timestamptz | |

Unique parziale su `(column_def_id, row_pk) WHERE status IN ('pending', 'claimed')`. Indice parziale su `(next_attempt_at, id)` per i `pending`, su `claimed_at` per i `claimed`. I job `done` si potano con `ai.prune_jobs(interval)`. Se la riga sorgente sparisce prima del claim, il job viene cancellato.

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
| source | ai.result_source | `model`, `human` |
| is_current | boolean | uno solo per riga e colonna |
| written | boolean | false se `hold` ha trattenuto il valore |
| model | text | modello che ha risposto davvero (può differire dal richiesto per fallback del provider) |
| usage | jsonb | token e costo riportati dal provider |
| latency_ms | integer | |
| details | jsonb | extra del backend: per `decision` le probabilità per classe (`probabilities`), la probabilità del vero (`probability_true`), il punteggio grezzo e la legenda, `questions_in_call` quando la chiamata era condivisa con altre colonne |
| created_at | timestamptz | |

Unique parziale su `(column_def_id, row_pk) WHERE is_current`. Indice su `(column_def_id, column_version_id)` per trovare le righe stale, e su `(column_def_id, created_at) WHERE NOT is_current` per la retention. `ai.prune` cancella le righe non correnti più vecchie di `lineage_retention_days`; le correnti restano sempre.

### spend

La spesa per colonna e giorno UTC, alimentata da `ai.complete_job` per ogni `result` da modello che registra (anche quelli scartati perché la riga era cambiata: sono stati pagati). Non da un trigger su `ai.result`, che scatterebbe anche durante un `pg_restore`. Resta anche quando il lineage verrà potato.

| Campo | Tipo | Note |
|---|---|---|
| column_def_id | bigint | FK, PK con `day` |
| day | date | giorno UTC del risultato |
| cost | numeric | somma di `usage.cost` riportato dal provider, in USD |
| results | bigint | risultati da modello contati |

Non conta le chiamate fallite dopo essere state pagate (output fuori schema): la spesa reale può superare questa di poco.

### schema_version

| Campo | Tipo | Note |
|---|---|---|
| version | integer | PK, numero del file SQL applicato |
| applied_at | timestamptz | |

## Viste

- `ai.columns`: definizioni con versione corrente espansa, conteggi di job per stato e stato del backfill (`backfill_pending`, `backfill_scanned`).
- `ai.stale_rows`: righe il cui risultato corrente ha una versione diversa dalla corrente della definizione, escluse le umane.
- `ai.needs_review`: risultati correnti da modello con `confidence < confidence_threshold`.
- `ai.dead_jobs`: job esauriti con ultimo errore.
- `ai.cost_by_column`: somma di token e costo per definizione e versione.
- `ai.budgets`: per definizione il tetto, il periodo, lo speso nel periodo corrente e in totale, il residuo e `exhausted`.

## Funzioni pubbliche

Gestione: `ai.add_column`, `ai.update_column` (nuova versione), `ai.configure` (policy senza versione), `ai.drop_column`, `ai.enable`, `ai.disable`, `ai.backfill`, `ai.unpin`, `ai.retry_dead`, `ai.prune_jobs`, `ai.spent(def_id, period)`. Ogni funzione ha un `COMMENT` leggibile con `\df+ ai.*`.

Contratto worker: `ai.claim_jobs(worker_id, batch_size, backends[])`, `ai.complete_job(job_id, source_hash, value, confidence, model, usage, latency_ms, details)` che restituisce `ai.complete_outcome` (`written`, `held`, `stale_requeued`, `cancelled`), `ai.fail_job(job_id, error, retryable)` che restituisce lo stato risultante, `ai.reclaim_stale(timeout)`, e per la manutenzione `ai.prune(limit, jobs_older_than)` che restituisce `(results, jobs)` cancellati, ciascuno al massimo `limit`. `claim_jobs` fa avanzare i backfill in corso prima e dopo aver preso i job, salta le definizioni con budget esaurito e aggiunge ai job `decision` scelti i job `decision` pronti delle stesse righe e dello stesso modello, quindi può restituire più di `batch_size` righe. Canale `NOTIFY ai_jobs` con l'id della definizione a ogni accodamento e a ogni `ai.configure` (alzare un budget sveglia subito i worker).

Trigger per tabella utente: `ai_enqueue_<column>` (AFTER INSERT OR UPDATE OF sorgenti) e `ai_override_<column>` (AFTER UPDATE OF target). Il secondo ignora le scritture fatte dentro `complete_job` (GUC `ai.writer = 'worker'`); un UPDATE a NULL fatto a mano toglie il pin e riaccoda la riga.

## Ruoli e privilegi

`ai_worker` (NOLOGIN, creato dall'install): `USAGE` sullo schema `ai`, `EXECUTE` su `claim_jobs`, `complete_job`, `fail_job`, `reclaim_stale`, `prune` (revocato a `PUBLIC`), `SELECT` su `ai.columns`, `ai.budgets`, `ai.dead_jobs`, `ai.needs_review`, `ai.stale_rows`, `ai.cost_by_column`. Le funzioni del contratto, `ai.prune` e le funzioni trigger `ai.enqueue_trigger` e `ai.override_trigger` sono `SECURITY DEFINER` con `search_path = pg_catalog, pg_temp`.

## Relazioni

`column_def` 1-N `column_version`; `column_def` 1-N `job`; `column_def` 1-N `result`; `column_version` 1-N `result`; `column_def` 1-N `spend`. Le tabelle dell'utente non hanno FK verso lo schema `ai`: il legame è per `row_pk`, e la cancellazione di una riga utente lascia il lineage orfano di proposito (storia).

## Migrazioni

File numerati in `worker/src/aicol/sql/` (`sql/` alla radice è un link), tracciati in `ai.schema_version`. Un file applicato non si riscrive. Due modi di applicarli, esclusivi fra loro:

- `aicol install`: una transazione per file, funziona su qualunque Postgres raggiungibile, gestiti inclusi.
- `CREATE EXTENSION aicol`: `aicol extension-files` genera `aicol.control`, `aicol--0.1.sql` dal file 0001 e uno script `aicol--0.(N-1)--0.N.sql` per ogni file successivo; Postgres li concatena sia all'installazione sia con `ALTER EXTENSION aicol UPDATE`. Ogni script marca tabelle e sequenze di `ai` (tranne `schema_version`) con `pg_extension_config_dump`, così `pg_dump` ne salva i dati. Richiede accesso alla `sharedir` del server, quindi solo Postgres self-hosted.

`aicol install` rifiuta un database dove l'estensione esiste già.
