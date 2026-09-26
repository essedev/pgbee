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
| current_version_id | bigint | FK `column_version`, nullable solo durante la creazione |
| config | jsonb | `batch_size`, `max_attempts`, `backoff_base_seconds`, `confidence_threshold`, `low_confidence_policy` (`write`/`hold`), `override_policy` (`pin`/`until_source_change`), `concurrency` |
| enabled | boolean | disabilitata: i trigger restano ma non accodano |
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
| details | jsonb | extra del backend: per `decision` le probabilità per classe (`probabilities`), la probabilità del vero (`probability_true`), il punteggio grezzo e la legenda |
| created_at | timestamptz | |

Unique parziale su `(column_def_id, row_pk) WHERE is_current`. Indice su `(column_def_id, column_version_id)` per trovare le righe stale.

### schema_version

| Campo | Tipo | Note |
|---|---|---|
| version | integer | PK, numero del file SQL applicato |
| applied_at | timestamptz | |

## Viste

- `ai.columns`: definizioni con versione corrente espansa e conteggi di job per stato.
- `ai.stale_rows`: righe il cui risultato corrente ha una versione diversa dalla corrente della definizione, escluse le umane.
- `ai.needs_review`: risultati correnti da modello con `confidence < confidence_threshold`.
- `ai.dead_jobs`: job esauriti con ultimo errore.
- `ai.cost_by_column`: somma di token e costo per definizione e versione.

## Funzioni pubbliche

Gestione: `ai.add_column`, `ai.update_column` (nuova versione), `ai.configure` (policy senza versione), `ai.drop_column`, `ai.enable`, `ai.disable`, `ai.backfill`, `ai.unpin`, `ai.retry_dead`, `ai.prune_jobs`. Ogni funzione ha un `COMMENT` leggibile con `\df+ ai.*`.

Contratto worker: `ai.claim_jobs(worker_id, batch_size, backends[])`, `ai.complete_job(job_id, source_hash, value, confidence, model, usage, latency_ms, details)` che restituisce `ai.complete_outcome` (`written`, `held`, `stale_requeued`, `cancelled`), `ai.fail_job(job_id, error, retryable)` che restituisce lo stato risultante, `ai.reclaim_stale(timeout)`. Canale `NOTIFY ai_jobs` con l'id della definizione a ogni accodamento.

Trigger per tabella utente: `ai_enqueue_<column>` (AFTER INSERT OR UPDATE OF sorgenti) e `ai_override_<column>` (AFTER UPDATE OF target). Il secondo ignora le scritture fatte dentro `complete_job` (GUC `ai.writer = 'worker'`); un UPDATE a NULL fatto a mano toglie il pin e riaccoda la riga.

## Relazioni

`column_def` 1-N `column_version`; `column_def` 1-N `job`; `column_def` 1-N `result`; `column_version` 1-N `result`. Le tabelle dell'utente non hanno FK verso lo schema `ai`: il legame è per `row_pk`, e la cancellazione di una riga utente lascia il lineage orfano di proposito (storia).

## Migrazioni

File numerati in `sql/`, applicati in ordine da `aicol install` (una transazione per file), tracciati in `ai.schema_version`. Un file applicato non si riscrive.
