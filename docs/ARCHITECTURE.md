# Architettura

## Visione d'insieme

Il progetto aggiunge a PostgreSQL le colonne derivate da modello: l'utente dichiara una volta che `ticket.urgency` si ricava da `ticket.body` con un prompt e un modello, e da quel momento ogni riga nuova o modificata riceve il suo valore senza codice applicativo. Il database possiede la garanzia (coda transazionale, retry, versioni, lineage, override umano); un processo esterno esegue le chiamate al modello. Il contesto e la concorrenza stanno in `ANALYSIS.md`.

## Componenti

- **Estensione SQL** (`sql/`): schema `ai` con catalogo delle colonne derivate, versioni, coda dei job, lineage dei risultati, trigger sulle tabelle utente, funzioni del contratto worker. SQL e PL/pgSQL puri, nessun codice compilato: si installa su qualunque Postgres 15+, gestito incluso. È la sorgente di verità del modello.
- **Worker** (`worker/`): processo Python (pacchetto `aicol`) con CLI. Installa e aggiorna l'estensione (`aicol install`), esegue il ciclo di lavoro (`aicol run`), mostra lo stato (`aicol status`). Parla con i modelli via OpenRouter. Non conosce le tabelle dell'utente: riceve dai job i valori sorgente già letti dal database e restituisce valori.
- **Demo** (`demo/`): Postgres in Docker, una tabella di ticket di assistenza in italiano con dati realistici, quattro colonne derivate (classificazione, categoria, riassunto, estrazione strutturata), script che esercita il ciclo completo. Più avanti una piccola UI che mostra le celle riempirsi.

## Flusso di una riga

1. `INSERT INTO ticket (body) VALUES (...)`. Il trigger installato da `ai.add_column` calcola l'hash SHA-256 dei valori delle colonne sorgente e inserisce un job `pending` con la chiave primaria della riga e l'hash. Se esiste già un job vivo per la stessa riga e colonna, aggiorna l'hash di quello. Tutto nella transazione dell'insert: o entra la riga con il suo job, o niente.
2. Il worker riceve la `NOTIFY` sul canale `ai_jobs` (o si sveglia al poll interval) e chiama `ai.claim_jobs(worker_id, batch_size)`. La funzione seleziona job pronti con `FOR UPDATE SKIP LOCKED`, li marca `claimed`, e restituisce per ciascuno i valori sorgente letti con SQL dinamico più la versione della definizione (prompt, modello, schema di output). Il worker raggruppa per definizione e manda le richieste in parallelo, con un limite di concorrenza per provider.
3. Il worker chiama il modello con output strutturato (JSON schema derivato dal tipo dichiarato) e riceve `{value, confidence}`.
4. `ai.complete_job(job_id, source_hash, value, confidence, model, usage, latency_ms)`: verifica che l'hash processato coincida con quello corrente del job (la riga può essere cambiata nel frattempo), valida il valore rispetto al tipo, scrive la colonna target con SQL dinamico dentro un guard (`SET LOCAL ai.writer = 'worker'`) così il trigger di override non lo prende per una correzione umana, inserisce il risultato nel lineage marcandolo corrente, chiude il job. Se l'hash non coincide, il risultato entra nel lineage come non corrente e il job torna `pending`.
5. In caso di errore il worker chiama `ai.fail_job(job_id, error, retryable)`: se retryable e sotto il massimo tentativi, il job torna `pending` con `next_attempt_at` esponenziale; altrimenti diventa `dead`, visibile in `ai.dead_jobs` e recuperabile con `ai.retry_dead`. Un worker che muore lascia job `claimed`: `ai.reclaim_stale` li rimette in coda dopo il timeout.

## Cambio di prompt o modello

`ai.update_column(...)` crea una nuova riga in `ai.column_version` e la rende corrente. I risultati esistenti puntano alla versione con cui sono stati calcolati, quindi lo stato "stale" non è una colonna da mantenere: è la differenza tra versione corrente e versione del risultato. La funzione accoda un job per ogni riga con risultato non corrente o assente, saltando le righe con valore umano. Il ricalcolo è incrementale per costruzione.

## Override umano

Un `UPDATE ticket SET urgency = 'low'` fuori dal guard del worker scatta il trigger di override: inserisce un risultato con `source = 'human'`, lo marca corrente, cancella i job vivi per quella riga. Da lì la riga è pinnata: né il cambio prompt né il cambio delle sorgenti la ricalcolano, salvo policy `until_source_change` sulla definizione. `ai.unpin(table, column, pk)` toglie il pin e riaccoda; lo stesso effetto si ottiene con un `UPDATE` che mette la colonna a NULL, il gesto naturale per dire "ricalcola". L'insieme degli override umani è anche la base dei few-shot futuri (vedi ROADMAP).

## Confidenza

I modelli non danno confidenza calibrata. Con il backend `llm` la v1 chiede al modello di auto-valutarla nel JSON di output; con `decision` è la probabilità calibrata della classe scelta. In entrambi i casi finisce in `ai.result.confidence` e la espone nella vista `ai.needs_review` (righe sotto la soglia della definizione). Policy `low_confidence`: `write` (default, scrive comunque) o `hold` (lascia la colonna a NULL e tiene il risultato solo nel lineage finché un umano non conferma). Il numero è un'euristica e la documentazione lo dice.

## Identità delle righe

L'estensione richiede una chiave primaria sulla tabella target e la legge da `pg_constraint` al momento di `ai.add_column`. La chiave viaggia come `jsonb` (`{"id": 42}`) così bigint, uuid e chiavi composte passano dallo stesso codice. Tutto il SQL dinamico usa `format('%I')` per identificatori e parametri per i valori.

## Backend

Ogni versione di una definizione dichiara un `backend`, cioè chi produce il valore (decisione #10 e #12):

- `llm`: un modello di linguaggio con output strutturato. Il worker costruisce il JSON schema dal tipo dichiarato, manda prompt e sorgenti, riceve valore e confidenza. Una riga per chiamata, concorrenza limitata.
- `decision`: un modello a risposta tipizzata (TypeSafe Jev, tramite l'endpoint `decisions` di OpenRouter). Niente generazione: la colonna diventa una domanda (scelta fra classi con descrizioni, vero/falso, punteggio su rubrica) e la risposta porta probabilità calibrate, salvate in `ai.result.details`. Solo `enum`, `boolean`, `integer`, `numeric`. Un ordine di grandezza più veloce ed economico di un LLM sulla classificazione.
- `embedding`: un modello di embedding. Il worker manda le sorgenti a lotti (centinaia per chiamata) e riceve un vettore per riga, scritto in una colonna `vector` di pgvector con la dimensione dichiarata. Nessuna confidenza.
- `custom`: il valore lo calcola un worker scritto dall'utente (geocoding, OCR, servizio interno) che consuma i job delle sue definizioni con lo stesso contratto. L'estensione non sa né le importa come.

I quattro backend condividono tutto il resto: coda, hash, versioni, ricalcolo selettivo, lineage, override. Cambiare modello di embedding e rifare i vettori è un `update_column` come cambiare un prompt.

## Tipi di output della v1

`enum` (lista di valori ammessi, colonna `text` validata in `complete_job`), `text`, `boolean`, `integer`, `numeric`, `jsonb` (con JSON schema validato dal worker), `vector` (dimensione dichiarata, richiede pgvector). Il `vector` è uno a uno con la riga: il chunking con più vettori per riga è rinviato (`DECISIONS.md` #11).

## Contratto worker

Le funzioni `ai.claim_jobs`, `ai.complete_job`, `ai.fail_job`, `ai.reclaim_stale` e il canale `ai_jobs` sono l'interfaccia pubblica. Un worker in qualunque linguaggio che rispetta questo contratto è un worker valido: chi deve passare da un gateway interno scrive il suo. Il worker Python è l'implementazione di riferimento, non l'unica.

## Decisioni chiave

Le decisioni con alternativa scartata stanno numerate in `DECISIONS.md`. Le principali: SQL puro invece di estensione compilata (#1), worker esterno invece di chiamate dal DB (#2), Python per il worker di riferimento con Rust rinviato (#3), stale calcolato dalle versioni invece che memorizzato (#4), override pinnato di default (#5), confidenza auto-riportata come euristica dichiarata (#6), psycopg senza ORM nel worker (#7), perimetro a tre backend senza job system (#10), embedding uno a uno (#11).

## Boundary

- L'estensione non fa rete, non conosce provider né prompt template: conserva testo e configurazione, li consegna al worker.
- Il worker non conosce lo schema dell'utente: lavora solo con le tabelle e le funzioni dello schema `ai`.
- La demo non contiene logica: usa estensione e worker come li userebbe un utente. Se la demo ha bisogno di una scorciatoia, manca qualcosa al prodotto.
- Le tabelle dell'utente ricevono solo la colonna target. Stato, versioni, confidenza e costi si leggono dallo schema `ai`, mai da colonne ausiliarie aggiunte alla tabella.

## Sicurezza e permessi

Le funzioni sono `SECURITY INVOKER`: il ruolo del worker deve avere `SELECT` sulle colonne sorgente e `UPDATE` sulle colonne target delle tabelle coinvolte, oltre ai privilegi sullo schema `ai`. `ai.add_column` documenta i grant necessari. Il prompt è dato, non codice: viaggia nel catalogo, non viene mai interpolato in SQL.

## Tradeoff accettati

- Le colonne derivate sono eventualmente consistenti e il modello lo dichiara: fra insert e valore passa il tempo di un batch.
- Il backfill di una tabella grande accoda tutte le righe in una transazione: bene fino a qualche milione di righe, oltre serve un backfill a lotti (ROADMAP).
- Il costo per riga dipende dal provider ed è visibile a posteriori in `ai.result.usage`; non c'è ancora un budget preventivo per definizione.
