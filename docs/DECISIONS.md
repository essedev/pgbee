# Decisioni

Voci numerate, citabili come `#N`. Entra solo ciò che vincola il futuro con un'alternativa scartata. Status: proposta, attiva, superata.

## #1 Estensione in SQL puro, non compilata

Status: attiva per la scelta SQL puro contro codice compilato; il rinvio di `CREATE EXTENSION` è superato da #18, che genera l'estensione dagli stessi file. L'estensione è SQL più PL/pgSQL, installata da uno script versionato e non da `CREATE EXTENSION`. Alternativa scartata: Rust con pgrx. Motivo: i Postgres gestiti non caricano estensioni compilate arbitrarie, e tutto ciò che serve alla v1 (tabelle, trigger, funzioni, `SKIP LOCKED`, `NOTIFY`) è disponibile in SQL. Il packaging come estensione vera e l'accesso al planner sono lavoro della fase prodotto, se ci si arriva.

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

Status: attiva. Il worker usa psycopg 3 async e SQL esplicito verso le funzioni dello schema `bee`. Alternativa scartata: SQLAlchemy 2.0 async con Alembic, lo stack di default. Motivo: lo schema lo possiede l'estensione, non il Python; un ORM duplicherebbe il modello e Alembic sarebbe una seconda sorgente di migrazioni.

## #8 Prompt senza template

Status: attiva. Il prompt è l'istruzione; il worker aggiunge le sorgenti in un blocco fisso `nome_colonna: valore`. Alternativa scartata: placeholder `{{colonna}}` nel prompt. Motivo: versioni più semplici, hash delle sorgenti indipendente dal prompt, nessun motore di template da mantenere. Si riapre se un caso reale lo richiede.

## #9 Documentazione interna in italiano, README in inglese

Status: attiva. `docs/` e `CLAUDE.md` in italiano, `README.md`, codice, SQL e commenti in inglese. Alternativa scartata: tutto in inglese da subito. Motivo: oggi l'unico lettore della doc interna è italiano; il README è la faccia pubblica di un tool pensato per essere open source. Si traduce quando il progetto apre.

## #10 Perimetro: colonne derivate con backend espliciti, mai un job system

Status: attiva, estesa da #12 (quarto backend `decision`). Una definizione dichiara un `backend`: `llm` (output strutturato da un modello di linguaggio), `embedding` (vettore da un modello di embedding), `custom` (il valore lo calcola un worker scritto dall'utente, per geocoding, OCR, servizi interni). Ogni job esiste perché esiste una riga con un valore da mantenere. Alternativa scartata: evolvere la coda in un job system generico su Postgres (job senza riga di destinazione, cron, workflow, dipendenze). Motivo: pgmq, pg-boss, Graphile Worker, River e Oban presidiano quel mercato da anni; il valore di questo progetto è la semantica sopra la coda (stale, lineage, override, dedup per hash, ricalcolo selettivo), che una coda generica non ha. Il backend `custom` costa un campo nel catalogo perché il contratto worker è già indipendente dal backend. Lo schema resta `bee`: il meccanismo è generico, il caso d'uso che vende è quello.

## #11 Embedding uno a uno nella v1, chunking dopo

Status: attiva. Il backend `embedding` produce un vettore per riga in una colonna `vector` (pgvector), con dimensione dichiarata nello schema di output. Alternativa scartata: chunking del testo con N vettori per riga in una tabella collegata, come il vectorizer di pgai. Motivo: il chunking introduce una destinazione a tabella e rompe il modello "una colonna"; per l'esperimento i testi sono corti e il vettore per riga basta. Se l'esperimento passa, la destinazione a tabella entra come campo nuovo della definizione, senza toccare coda e lineage.

## #12 Backend `decision` per i modelli a risposta tipizzata

Status: attiva. I modelli di decisione (TypeSafe Jev, via l'endpoint `decisions` di OpenRouter) non generano testo: rispondono a domande tipizzate (scelta, vero/falso, punteggio su rubrica) con probabilità calibrate. Sono un backend a sé, `decision`, limitato a `enum`, `boolean`, `integer` e `numeric`, con i criteri delle classi nell'`output_schema` e le probabilità per classe salvate in `bee.result.details`. Alternativa scartata: trattarli come provider del backend `llm` scelto dal prefisso del modello. Motivo: il contratto è diverso (nessuna generazione, criteri obbligatori, confidenza vera), e nasconderlo dietro `llm` avrebbe reso la confidenza ambigua fra euristica e calibrata. `complete_job` guadagna un parametro finale opzionale `p_details`: aggiunta compatibile con i worker esistenti.

## #13 Tetto di spesa per colonna, contato nel database, applicato al claim

Status: attiva. Ogni definizione può avere `budget_usd` per giorno, mese o in totale. La spesa si conta in `bee.spend` dentro `bee.complete_job` (il costo lo riporta il provider nell'usage; fino allo 0009 lo faceva un trigger su `bee.result`, spostato per #18) e il controllo sta in `bee.claim_jobs`: nessuna modifica al contratto del worker, e vale per qualunque worker, anche di terzi. Alternative scartate: budget nel worker (ogni worker dovrebbe reimplementarlo e più worker non vedrebbero la spesa degli altri); somma su `bee.result` a ogni claim (costo proporzionale al lineage, e sparirebbe con la potatura); stima preventiva del costo in `add_column` (il database non conosce i prezzi). Default senza tetto per non fermare in silenzio colonne esistenti; la demo lo imposta. Limiti noti: sforamento massimo di un batch in volo per worker, e le chiamate pagate ma fallite (output fuori schema) non vengono contate.

## #14 I job `decision` della stessa riga si reclamano insieme

Status: attiva. Il worker manda in una chiamata sola le domande `decision` di una riga (stesso modello, stesse sorgenti), perché Jev fa pagare soprattutto il testo: misurato su un ticket, una domanda 0.0000135 USD, tre domande insieme 0.0000199 contro 0.0000406 separate. Nel backfill però i job delle diverse colonne sono lontani in coda e finirebbero in batch diversi, quindi `bee.claim_jobs` aggiunge ai job `decision` scelti i fratelli pronti sulle stesse righe e con lo stesso modello. Alternative scartate: ordinare la coda per riga (rompe il FIFO fra colonne e tabelle); una funzione di claim separata per i fratelli (un secondo giro e una firma in più nel contratto). Conseguenza sul contratto: un claim può restituire più di `batch_size` job, al massimo `batch_size` per il numero di colonne `decision` della tabella. Nessuna firma cambia.

## #15 Backfill a chunk con cursore, guidato da `claim_jobs`

Status: attiva. `bee.backfill` accoda il primo chunk in ordine di chiave primaria e salva un cursore in `column_def`; i chunk successivi li accoda `bee.claim_jobs` quando la coda della colonna scende sotto un chunk. Misurato su 1M righe: `add_column` da 39 s a 0.06 s, un insert concorrente bloccato da 38.9 s a 0.01 s (`ALTER TABLE` tiene il lock fino al commit, quindi prima il blocco durava tutta la scansione), primo claim da 2 s a 27 ms (che include il rabbocco del chunk successivo). Alternative scartate: una procedura con `COMMIT` per chunk (non si chiama dentro la transazione di `add_column` e qualcuno deve lanciarla); un passo di backfill esplicito nel worker (funzione in più nel contratto, e i worker di terzi dovrebbero ricordarsi di chiamarla); `pg_cron` (dipendenza esterna). Conseguenze: `bee.backfill` e `bee.enable` restituiscono i job accodati dal primo chunk, non il totale; senza un worker attivo la scansione non avanza; un claim che fa avanzare un backfill costa qualche decina di millisecondi in più.

## #16 Worker con privilegi minimi tramite funzioni `SECURITY DEFINER`

Status: attiva. Il worker ha solo il ruolo `bee_worker`: `EXECUTE` sul contratto e `SELECT` sulle viste. Le funzioni del contratto e i trigger girano come il proprietario dell'estensione, con `search_path` fissato, e `EXECUTE` sul contratto è revocato a `PUBLIC`. Alternativa scartata: funzioni `SECURITY INVOKER` con grant al worker su ogni tabella utente (colonne sorgente in lettura, target in scrittura, tabelle `bee` in scrittura). Il worker avrebbe visto intere tabelle, i grant andavano rifatti per ogni colonna dichiarata, e ogni ruolo applicativo avrebbe avuto bisogno di scrivere in `bee.job`. Rischio accettato: una funzione definer è un confine di sicurezza, quindi ogni ridefinizione deve ripetere gli attributi (rule SQL e `test_roles.py`), e l'install richiede `CREATEROLE` oppure un ruolo creato prima da un superuser.

## #17 Retention del lineage per colonna, eseguita dal worker

Status: attiva. Ogni colonna può fissare `lineage_retention_days`; `bee.prune` cancella a lotti i risultati non correnti più vecchi e i job `done` oltre una settimana, e il worker di riferimento la chiama ogni ora. Il default resta conservare tutto, perché il lineage è la ragione d'essere dell'estensione e cancellarlo deve essere una scelta. Alternative scartate: `pg_cron` (dipendenza esterna, spesso assente sui Postgres gestiti); lasciare la chiamata all'operatore (il principio del progetto è che l'invariante vive nel codice, non in un cron da ricordare); partizionare `bee.result` per tempo (più efficiente sui volumi grandi, ma la retention è per colonna e i risultati correnti non scadono mai, quindi una partizione intera non si può staccare). `bee.prune` entra nel contratto del worker come funzione nuova, nessuna firma esistente cambia.

## #18 Due modi di installare, generati dagli stessi file

Status: attiva. `pgbee install` applica i file SQL con una connessione qualunque e resta il modo per i Postgres gestiti (RDS, Supabase, Neon, Cloud SQL), dove non si possono copiare file nel server. `CREATE EXTENSION pgbee` è per i server self-hosted: `pgbee extension-files` genera control file e script di versione dagli stessi file, un file per versione, così i due modi non possono divergere. I file SQL stanno nel pacchetto del worker e viaggiano nel wheel. Alternative scartate: solo l'estensione (escluderebbe i database gestiti, la maggior parte dei casi reali); script di estensione scritti a mano (una seconda copia del modello, da tenere allineata); pubblicazione su PGXN o registrazione via `pg_tle` (possibile perché l'estensione è SQL puro, rinviata finché non c'è un utente che la chiede). Il giro di prova dump e restore ha trovato due difetti che nell'installazione con `pgbee install` non emergevano, corretti nello 0010: la FK circolare fra `column_def` e `column_version`, e il trigger che contava la spesa su `bee.result`, ora dentro `complete_job`. Il rischio di convivenza con pgai (stesso schema `ai`) è stato chiuso dalla #19.

## #19 Nome: pgbee, schema `bee`

Status: attiva. Il progetto, il pacchetto Python, la CLI, l'estensione e le immagini si chiamano `pgbee`; dentro il database lo schema è `bee` (`bee.add_column`), il ruolo `bee_worker`, il canale `bee_jobs`, la variabile di sessione `bee.writer`. È lo schema di pg_cron (estensione `pg_cron`, funzioni in `cron`): il prefisso `pg` dice dove vive la cosa e rende il nome unico sui registri (PyPI, npm e PGXN liberi al momento della scelta), lo schema resta corto da scrivere. La mascotte è l'ape operaia che riempie le celle; "AI" sta nella riga di presentazione, non nel nome, perché la tesi è che il modello sia un backend e non il prodotto. Alternative scartate: `ai` (lo stesso schema di pgai, quindi impossibile da installare accanto, e il nome che chiunque vorrebbe), `aicol` (si legge come "alcool"), `derive`, `enrich`, `dcol` (descrittivi ma senza identità). Per rinominare sono stati riscritti i file SQL già committati, eccezione unica alla regola "un file applicato non si riscrive": `ALTER SCHEMA ... RENAME` non aggiorna i riferimenti dentro i corpi delle funzioni, e al momento l'unica installazione esistente era quella locale della demo e dei test. Da qui in poi la regola vale senza eccezioni.

## #20 Un ciclo e una connessione per backend nel worker

Status: attiva. Il worker di riferimento è un pool: per ogni backend servito apre una connessione e fa girare un ciclo che reclama solo i job di quel backend. La prova sul campo ha mostrato il difetto del ciclo unico: un batch misto finisce quando finisce la chiamata più lenta, quindi Jev (305 ms) ed embedding (33 ms) avanzavano al passo dell'LLM (1,9 s), 749 e 751 s contro 967 s su 3000 righe; col pool, su 600 righe, 28 e 21 s contro 144 s. Le connessioni sono separate perché una connessione in attesa di `NOTIFY` non può eseguire query. Alternative scartate: un'impostazione per tornare al ciclo unico (nessuno vorrebbe l'attesa fra backend: un parametro senza caso d'uso); un claim che restituisce job di tutti i backend ma li processa senza attendere il batch (lo stesso risultato con più stato da gestire). L'unica opzione esposta è `--backends`, perché permette di scalare i backend separatamente. Nessuna modifica al database: `claim_jobs` filtrava già per backend.

## #21 Licenza Apache-2.0, progetto personale su `essedev`

Status: attiva. pgbee esce come progetto personale dell'autore, sul GitHub `essedev`, con licenza Apache-2.0. Motivo: è la licenza abituale dell'infrastruttura open source, a differenza di MIT concede esplicitamente l'uso dei brevetti (conta per un'azienda che valuta di adottarla) e lascia aperta un'eventuale offerta commerciale. Alternative scartate: AGPL o licenze tipo Elastic (proteggono dai cloud provider ma frenano l'adozione, che in questa fase è ciò che va misurato); licenza PostgreSQL come pgvector (adatta all'estensione, meno al worker Python e al resto del repository); MIT (equivalente in pratica, senza la clausola sui brevetti).
