# Decisioni

Voci numerate, citabili come `#N`. Entra solo ciò che vincola il futuro con un'alternativa scartata. Status: proposta, attiva, superata.

## #1 Estensione in SQL puro, non compilata

Status: attiva. L'estensione è SQL più PL/pgSQL, installata da uno script versionato e non da `CREATE EXTENSION`. Alternativa scartata: Rust con pgrx. Motivo: i Postgres gestiti non caricano estensioni compilate arbitrarie, e tutto ciò che serve alla v1 (tabelle, trigger, funzioni, `SKIP LOCKED`, `NOTIFY`) è disponibile in SQL. Il packaging come estensione vera e l'accesso al planner sono lavoro della fase prodotto, se ci si arriva.

## #2 Il database non chiama i provider

Status: attiva. Le chiamate ai modelli le fa un worker esterno che consuma la coda. Alternativa scartata: `pg_net`, estensione `http` o background worker in-process, come pgai e postgres-llm. Motivo: governance (il DB come client di rete verso terzi è vietato quasi ovunque), rate limit e latenza fuori dalla transazione, contratto pubblico che permette worker propri dietro un gateway.

## #3 Worker di riferimento in Python, Rust rinviato

Status: attiva. Il worker è un pacchetto Python con `uv`, psycopg 3 async e SDK OpenAI puntato a OpenRouter. Alternativa scartata: Rust o Go da subito, che darebbero il binario singolo che il mercato si aspetta. Motivo: il progetto è un esperimento a tempo e il contratto SQL è identico; il worker si riscrive senza toccare l'estensione se l'esperimento passa.

## #4 Stale calcolato dalle versioni, non memorizzato

Status: attiva. Ogni risultato riferisce la `column_version` che l'ha prodotto; una riga è stale quando quella versione non è la corrente. Alternativa scartata: colonna di stato per riga aggiornata al cambio prompt. Motivo: nessuno stato da tenere sincronizzato, il ricalcolo incrementale è una query, e il lineage per riga viene gratis.

## #5 Override umano pinnato di default

Status: attiva. Un valore scritto da un umano non viene più ricalcolato, né al cambio prompt né al cambio sorgenti, finché non si toglie il pin. Alternativa scartata: ricalcolare al cambio delle sorgenti (disponibile come policy `until_source_change`). Motivo: chi corregge a mano vuole che la correzione resti; perdere una correzione umana costa più di un valore vecchio.

## #6 Confidenza auto-riportata, dichiarata euristica

Status: attiva per il backend `llm`; il backend `decision` (#12) restituisce probabilità calibrate e non rientra nell'euristica. La confidenza è un numero che il modello include nell'output strutturato. Alternativa scartata: logprobs, non disponibili in modo uniforme via OpenRouter; oppure niente confidenza. Motivo: serve un segnale per la coda di revisione e per la policy `hold`; un'euristica dichiarata è meglio dell'assenza, purché la documentazione non la spacci per probabilità.

## #7 psycopg senza ORM nel worker

Status: attiva. Il worker usa psycopg 3 async e SQL esplicito verso le funzioni dello schema `ai`. Alternativa scartata: SQLAlchemy 2.0 async con Alembic, lo stack di default. Motivo: lo schema lo possiede l'estensione, non il Python; un ORM duplicherebbe il modello e Alembic sarebbe una seconda sorgente di migrazioni.

## #8 Prompt senza template

Status: attiva. Il prompt è l'istruzione; il worker aggiunge le sorgenti in un blocco fisso `nome_colonna: valore`. Alternativa scartata: placeholder `{{colonna}}` nel prompt. Motivo: versioni più semplici, hash delle sorgenti indipendente dal prompt, nessun motore di template da mantenere. Si riapre se un caso reale lo richiede.

## #9 Documentazione interna in italiano, README in inglese

Status: attiva. `docs/` e `CLAUDE.md` in italiano, `README.md`, codice, SQL e commenti in inglese. Alternativa scartata: tutto in inglese da subito. Motivo: oggi l'unico lettore della doc interna è italiano; il README è la faccia pubblica di un tool pensato per essere open source. Si traduce quando il progetto apre.

## #10 Perimetro: colonne derivate con backend espliciti, mai un job system

Status: attiva, estesa da #12 (quarto backend `decision`). Una definizione dichiara un `backend`: `llm` (output strutturato da un modello di linguaggio), `embedding` (vettore da un modello di embedding), `custom` (il valore lo calcola un worker scritto dall'utente, per geocoding, OCR, servizi interni). Ogni job esiste perché esiste una riga con un valore da mantenere. Alternativa scartata: evolvere la coda in un job system generico su Postgres (job senza riga di destinazione, cron, workflow, dipendenze). Motivo: pgmq, pg-boss, Graphile Worker, River e Oban presidiano quel mercato da anni; il valore di questo progetto è la semantica sopra la coda (stale, lineage, override, dedup per hash, ricalcolo selettivo), che una coda generica non ha. Il backend `custom` costa un campo nel catalogo perché il contratto worker è già indipendente dal backend. Lo schema resta `ai`: il meccanismo è generico, il caso d'uso che vende è quello.

## #11 Embedding uno a uno nella v1, chunking dopo

Status: attiva. Il backend `embedding` produce un vettore per riga in una colonna `vector` (pgvector), con dimensione dichiarata nello schema di output. Alternativa scartata: chunking del testo con N vettori per riga in una tabella collegata, come il vectorizer di pgai. Motivo: il chunking introduce una destinazione a tabella e rompe il modello "una colonna"; per l'esperimento i testi sono corti e il vettore per riga basta. Se l'esperimento passa, la destinazione a tabella entra come campo nuovo della definizione, senza toccare coda e lineage.

## #12 Backend `decision` per i modelli a risposta tipizzata

Status: attiva. I modelli di decisione (TypeSafe Jev, via l'endpoint `decisions` di OpenRouter) non generano testo: rispondono a domande tipizzate (scelta, vero/falso, punteggio su rubrica) con probabilità calibrate. Sono un backend a sé, `decision`, limitato a `enum`, `boolean`, `integer` e `numeric`, con i criteri delle classi nell'`output_schema` e le probabilità per classe salvate in `ai.result.details`. Alternativa scartata: trattarli come provider del backend `llm` scelto dal prefisso del modello. Motivo: il contratto è diverso (nessuna generazione, criteri obbligatori, confidenza vera), e nasconderlo dietro `llm` avrebbe reso la confidenza ambigua fra euristica e calibrata. `complete_job` guadagna un parametro finale opzionale `p_details`: aggiunta compatibile con i worker esistenti.

## #13 Tetto di spesa per colonna, contato nel database, applicato al claim

Status: attiva. Ogni definizione può avere `budget_usd` per giorno, mese o in totale. La spesa si conta in `ai.spend` da un trigger su `ai.result` (il costo lo riporta il provider nell'usage) e il controllo sta in `ai.claim_jobs`: nessuna modifica al contratto del worker, e vale per qualunque worker, anche di terzi. Alternative scartate: budget nel worker (ogni worker dovrebbe reimplementarlo e più worker non vedrebbero la spesa degli altri); somma su `ai.result` a ogni claim (costo proporzionale al lineage, e sparirebbe con la potatura); stima preventiva del costo in `add_column` (il database non conosce i prezzi). Default senza tetto per non fermare in silenzio colonne esistenti; la demo lo imposta. Limiti noti: sforamento massimo di un batch in volo per worker, e le chiamate pagate ma fallite (output fuori schema) non vengono contate.

## #14 I job `decision` della stessa riga si reclamano insieme

Status: attiva. Il worker manda in una chiamata sola le domande `decision` di una riga (stesso modello, stesse sorgenti), perché Jev fa pagare soprattutto il testo: misurato su un ticket, una domanda 0.0000135 USD, tre domande insieme 0.0000199 contro 0.0000406 separate. Nel backfill però i job delle diverse colonne sono lontani in coda e finirebbero in batch diversi, quindi `ai.claim_jobs` aggiunge ai job `decision` scelti i fratelli pronti sulle stesse righe e con lo stesso modello. Alternative scartate: ordinare la coda per riga (rompe il FIFO fra colonne e tabelle); una funzione di claim separata per i fratelli (un secondo giro e una firma in più nel contratto). Conseguenza sul contratto: un claim può restituire più di `batch_size` job, al massimo `batch_size` per il numero di colonne `decision` della tabella. Nessuna firma cambia.

## #15 Backfill a chunk con cursore, guidato da `claim_jobs`

Status: attiva. `ai.backfill` accoda il primo chunk in ordine di chiave primaria e salva un cursore in `column_def`; i chunk successivi li accoda `ai.claim_jobs` quando la coda della colonna scende sotto un chunk. Misurato su 1M righe: `add_column` da 39 s a 0.06 s, un insert concorrente bloccato da 38.9 s a 0.01 s (`ALTER TABLE` tiene il lock fino al commit, quindi prima il blocco durava tutta la scansione), primo claim da 2 s a 7 ms. Alternative scartate: una procedura con `COMMIT` per chunk (non si chiama dentro la transazione di `add_column` e qualcuno deve lanciarla); un passo di backfill esplicito nel worker (funzione in più nel contratto, e i worker di terzi dovrebbero ricordarsi di chiamarla); `pg_cron` (dipendenza esterna). Conseguenze: `ai.backfill` e `ai.enable` restituiscono i job accodati dal primo chunk, non il totale; senza un worker attivo la scansione non avanza; un claim che fa avanzare un backfill costa qualche decina di millisecondi in più.
