# Cicli

Un ciclo è un'unità di lavoro chiusa. Il file tiene gli ultimi ~15; i più vecchi vanno in `docs/archive/`. Le decisioni durevoli stanno in `DECISIONS.md`, le milestone aperte in `ROADMAP.md`.

## Ciclo 1 (26 settembre 2026): dall'idea alla demo end to end

**Obiettivo.** Verificare in una giornata se il modello "colonna derivata mantenuta dal database" regge un ciclo completo su dati realistici, prima di decidere se farne un prodotto.

**Fatto.**

- Analisi di mercato e concorrenza (`ANALYSIS.md`), overview HTML con pro e contro delle decisioni (istantanea archiviata in `archive/overview-2026-09-26.html`, non più mantenuta).
- Estensione SQL (`sql/0001`-`0003`): schema `bee` con catalogo, versioni, coda, lineage, trigger di accodamento e override, contratto worker, viste. `0002` corregge il tipo della vista dei costi, `0003` azzera tentativi e backoff dei job pending quando nasce una versione nuova.
- Worker di riferimento `pgbee` (install, run, status): `LISTEN` più poll, batch per backend, backend `llm` con output strutturato, `embedding` a lotti, classificazione degli errori, parse JSON tollerante, drain consapevole del backoff. 27 test di integrazione sull'estensione senza modelli, 11 sul worker con provider finto più uno marcato `llm`.
- Demo ticket di assistenza: 21 ticket, sei colonne derivate, otto passi che coprono coda, ticket nuovo, override che resta, cambio prompt con ricalcolo selettivo, ricerca dei simili via embedding, lineage e costi. Stima di spesa con conferma prima di chiamare i modelli.
- Confronto di otto modelli su `urgency` e `category` contro `gold.json` (`compare_models.py`, tabella in `ANALYSIS.md`): i modelli piccoli del 2026 sono equivalenti a Haiku 4.5 a un decimo del costo; default della demo spostato a `openai/gpt-6-luna` con effort low.
- Backend `decision` (`sql/0004`) per i modelli a risposta tipizzata: TypeSafe Jev via `POST /api/alpha/decisions` di OpenRouter, probabilità calibrate in `bee.result.details`, `complete_job` con parametro `p_details` compatibile. Sui ticket: 100 per cento, 28 volte meno costoso di Haiku 4.5, confidenza che scende davvero sui casi ambigui.

**Decisioni.** #1-#12 in `DECISIONS.md`, tutte prese in questo ciclo; #12 (backend `decision`) estende il perimetro di #10.

**Prossimo passo.** Valutare il criterio di uscita in `ANALYSIS.md` e decidere se fare M4 (demo UI). Aperto: `.env.example` non elenca `OPENROUTER_BASE_URL`, letta dal worker.

## Ciclo 2 (26 settembre 2026): tetto di spesa, chiamate Jev condivise, worker più solido

**Obiettivo.** Chiudere i punti deboli emersi dalla demo prima di valutare il criterio di uscita: spesa senza limite per colonna, domande `decision` pagate una chiamata per colonna, worker lento a svegliarsi e a fermarsi.

**Fatto.**

- Tetto di spesa per colonna (`sql/0005`): `budget_usd` e `budget_period` (`day`, `month`, `total`) nella config, tabella `bee.spend` alimentata da un trigger su `bee.result`, vista `bee.budgets`, funzione `bee.spent`. `bee.claim_jobs` salta le colonne esaurite, `bee.configure` sveglia i worker quando il tetto sale. Speso e residuo in `pgbee status`; la demo mette un tetto su `urgency`.
- Colonne `decision` della stessa riga e dello stesso modello in una chiamata Jev sola, fino a 16 domande: tre domande 0.0000199 USD contro 0.0000406 in tre chiamate. Usage ripartito fra i job, `questions_in_call` nei `details`, una risposta mancante fa fallire solo il suo job.
- `bee.claim_jobs` porta con sé i job `decision` pronti delle stesse righe (`sql/0006`), così la condivisione vale anche nel backfill; un claim può restituire più di `batch_size` job, nessuna firma cambia.
- Worker: le notifiche arrivate durante un batch si consumano tutte al risveglio (prima N insert davano N claim vuoti); `stop()` interrompe subito poll e pausa da rate limit.
- Test: provider finto e helper spostati in `tests/fakes.py`; test del demone (risveglio, backlog, stop, reclaim) e del budget; smoke test contro OpenRouter per `llm`, `decision` ed `embedding`, marcati `llm` (costo sotto 0.001 USD). `.env.example` ora elenca `OPENROUTER_BASE_URL`.

**Decisioni.** #13 (tetto di spesa contato nel database e applicato al claim), #14 (fratelli `decision` nello stesso claim).

**Prossimo passo.** Invariato dal ciclo 1: valutare il criterio di uscita in `ANALYSIS.md` e decidere se fare M4 (demo UI).

## Ciclo 3 (26 settembre 2026): backfill a chunk

**Obiettivo.** Togliere il limite dichiarato nei tradeoff: il backfill accodava tutta la tabella in una transazione, con la tabella utente bloccata in scrittura per l'intera scansione e un job per riga in coda.

**Fatto.**

- Backfill incrementale (`sql/0007`): `bee.backfill` accoda un chunk in ordine di chiave primaria (`backfill_chunk` nella config, default 1000, validato fra 1 e 100000) e salva un cursore in `column_def` (`backfill_pending`, `backfill_cursor`, `backfill_scanned`); `bee.claim_jobs` accoda i chunk successivi prima e dopo il claim finché la coda della colonna ha meno di un chunk pending (al massimo dieci per claim) e manda una `NOTIFY` finché la scansione non è finita. Colonne disabilitate o oltre budget non avanzano. Nessuna firma del contratto cambia.
- Su 1M righe `add_column` passa da 39 s con la tabella bloccata a 0.06 s.
- `bee.columns` e `pgbee status` mostrano lo stato della scansione.
- Test: sei sull'estensione (chunk e claim, righe già a posto saltate, chiavi composte e uuid, `update_column` a chunk con righe pinnate, colonne ferme, validazione) e uno sul demone, che completa un backfill a chunk senza aspettare il poll.

**Decisioni.** #15 (backfill a chunk con cursore, guidato da `claim_jobs`).

**Prossimo passo.** Invariato: valutare il criterio di uscita in `ANALYSIS.md` e decidere se fare M4 (demo UI).

## Ciclo 4 (26 settembre 2026): caricamenti massivi, privilegi minimi, retention del lineage

**Obiettivo.** Rendere l'estensione installabile in un database vero: misurare i caricamenti massivi, togliere al worker l'accesso alle tabelle utente, fermare la crescita senza limite di `bee.result` e `bee.job`.

**Fatto.**

- Caricamento massivo misurato su 1M righe (solo doc, tradeoff in `ARCHITECTURE.md`): 38 s con una colonna derivata attiva contro 3.7 s senza, trigger per statement misurato a 33 s e scartato; la ricetta è `bee.disable`, caricamento, `bee.enable` (6.7 s più 19 ms). Documentato anche il claim lento (1-3 s) finché l'autovacuum non aggiorna le statistiche di `bee.job`.
- Ruolo `bee_worker` (`sql/0008`): funzioni del contratto e trigger di accodamento e override diventano `SECURITY DEFINER` con `search_path` fissato, `EXECUTE` sul contratto revocato a `PUBLIC` e concesso al ruolo, `SELECT` sulle viste. Il worker non ha grant sulle tabelle utente; un ruolo applicativo che scrive solo la tabella accoda e registra override. Cast a `vector` qualificato con lo schema. Regola sulle ridefinizioni nella rule SQL.
- Retention del lineage (`sql/0009`): `lineage_retention_days` nella config (default null, conservare tutto), `bee.prune(limit, jobs_older_than)` cancella a lotti i risultati non correnti scaduti e i job `done` oltre una settimana, mai risultati correnti, job `dead` o `bee.spend`. Entra nel contratto worker; il worker di riferimento la chiama all'avvio e ogni `PGBEE_MAINTENANCE_INTERVAL_SECONDS` (default un'ora), un errore di manutenzione non ferma la coda.
- Test: quattro sui ruoli (`test_roles.py`, ruoli di cluster creati e rimossi dal modulo), cinque sulla retention, due sul demone (manutenzione all'avvio e a intervallo, errore che non ferma la coda).

**Decisioni.** #16 (privilegi minimi con funzioni definer), #17 (retention per colonna eseguita dal worker).

**Prossimo passo.** Invariato: valutare il criterio di uscita in `ANALYSIS.md` e decidere se fare M4 (demo UI).

## Ciclo 5 (26 settembre 2026): packaging, `CREATE EXTENSION`, immagine del worker

**Obiettivo.** Rendere l'estensione installabile fuori dal repository: sui Postgres gestiti con il worker, sui server self-hosted come estensione vera, e il worker eseguibile in un container.

**Fatto.**

- I file SQL passano in `worker/src/pgbee/sql/` (`sql` alla radice resta come link simbolico) e viaggiano nel wheel: `pgbee install` funziona fuori dal repository, `make build` produce wheel e sdist.
- `pgbee extension-files` genera `pgbee.control`, `pgbee--0.1.sql` e uno script di update per ogni file successivo, ciascuno con `pg_extension_config_dump` sulle tabelle di `bee` così `pg_dump` ne salva i dati. `make extension-image` costruisce l'immagine pgvector con i file, `make test-extension` prova create, catena di update da 0.1 e dump e restore su un container usa e getta (porta 4463). `pgbee install` rifiuta un database che ha già l'estensione.
- Il giro dump e restore ha trovato due difetti invisibili con `pgbee install`, corretti in `sql/0010`: la FK circolare fra `column_def` e `column_version`, e il trigger su `bee.result` che contava la spesa, ora dentro `bee.complete_job`. Regola nuova nella rule SQL: niente trigger fra tabelle di `bee`, niente FK circolari.
- Immagine Docker del worker (`make worker-image`): multi stage sull'immagine di uv, utente non root, `pgbee run` di default, configurazione solo da ambiente, `.env` esclusi dal build context.
- Test: quattro in `test_packaging.py` (catena dei file generati più tre marcati `extension`), uno sull'estensione che verifica che la versione corrente appartenga sempre alla sua colonna.

**Decisioni.** #18 (due modi di installare generati dagli stessi file); #1 aggiornata di conseguenza, #13 aggiornata sul punto in cui si conta la spesa.

**Prossimo passo.** Invariato: valutare il criterio di uscita in `ANALYSIS.md` e decidere se fare M4 (demo UI).

## Ciclo 6 (26 settembre 2026): nome `pgbee`, prova sul campo su testi veri, un ciclo per backend

**Obiettivo.** Dare al progetto un nome che conviva con pgai e misurare il meccanismo su un volume e su testi reali, non sui 21 ticket della demo.

**Fatto.**

- Rinomina: progetto, pacchetto, CLI, estensione e immagini diventano `pgbee`; nel database lo schema è `bee`, il ruolo `bee_worker`, il canale `bee_jobs`, la variabile `bee.writer`, i trigger `bee_enqueue_<colonna>` e `bee_override_<colonna>`. I file SQL già committati sono stati riscritti una volta sola, perché `ALTER SCHEMA ... RENAME` non aggiorna i corpi delle funzioni.
- Prova sul campo su 3000 reclami CFPB (`demo/cfpb/`): `prepare.py` estrae un campione stratificato su nove prodotti dalla copia Hugging Face del database pubblico, `field_test.py` lo carica in un database dedicato (`aidb_cfpb`), dichiara quattro colonne (prodotto con Jev e con gpt-6-luna, un flag Jev, embedding) e fa girare il processo vero `pgbee run`. Esito: 12.000 job, zero falliti, 0.41 USD, 17,5 minuti; Jev e LLM al 79 per cento di accordo con un'etichetta rumorosa, Jev a un quinto del costo per domanda; la confidenza di entrambi separa i casi dubbi, il che corregge la lettura fatta sui 21 ticket. Tabelle in `ANALYSIS.md`.
- Un ciclo e una connessione per backend: la prova ha mostrato Jev ed embedding finire insieme all'LLM (749 e 751 s contro 967 s) perché un batch misto aspetta la chiamata più lenta. `WorkerPool` fa girare un `Worker` per backend, solo il primo fa manutenzione e reclaim; `pgbee run --backends` sceglie quali servire. Su 600 righe Jev finisce in 28 s, l'embedding in 21, l'LLM in 144. Nessuna modifica al database.
- Test: tre sul pool (backend veloce che non aspetta il lento, pool limitato ai suoi backend, validazione di `--backends`).

**Decisioni.** #19 (nome `pgbee`, schema `bee`, unica eccezione alla regola sui file SQL già committati), #20 (un ciclo e una connessione per backend).

**Prossimo passo.** Valutare il criterio di uscita in `ANALYSIS.md`, ora con i dati della prova sul campo, e decidere se fare M4 (demo UI).

## Ciclo 7 (26 settembre 2026): review esterna, risultato sotto la versione del claim

**Obiettivo.** Chiudere i difetti emersi da una review esterna del progetto (Codex, gpt-6-astra) prima di decidere che cosa farne.

**Fatto.**

- Bug di lineage corretto in `sql/0011`, riprodotto prima di toccarlo: `bee.complete_job` registrava ogni risultato sotto la versione corrente al completamento, quindi un `update_column` arrivato mentre un job era in calcolo faceva passare il valore del vecchio prompt per la versione nuova, contato come aggiornato e mai ricalcolato. `bee.claim_jobs` salva la versione nel job (`bee.job.claimed_version_id`); `complete_job` tratta un cambio di versione come un cambio delle sorgenti: risultato nel lineage sotto la versione vera, non corrente, riga di nuovo in coda. I job reclamati prima dello 0011 ricadono sulla versione corrente. Nessuna firma del contratto cambia; il test copre il caso.
- `demo/cfpb/field_test.py` stampa la stima (0.000136 USD per riga, misurata sulla prova completa) e chiede conferma prima di spendere, `--yes` salta la domanda, come `demo/run.py`.
- `ANALYSIS.md`: il 79 per cento della prova sul campo è accordo con un'etichetta rumorosa, non un limite inferiore dimostrato dell'accuratezza.

**Decisioni.** Nessuna nuova: lo 0011 ripristina l'invariante di #4 (ogni risultato riferisce la versione che l'ha prodotto). La review ha proposto anche un piano di validazione in quattro settimane, tenuto fuori dal repository come proposta in attesa di verdetto.

**Prossimo passo.** Decidere sul piano di validazione; se passa, sostituisce la valutazione del criterio di uscita in `ANALYSIS.md` fatta solo sui dati propri.

## Ciclo 8 (26 settembre 2026): preparazione alla pubblicazione

**Obiettivo.** Rendere il repository pubblicabile come progetto personale: licenza, CI, un avvio rapido per chi arriva da fuori, doc per l'utente in inglese.

**Fatto.**

- Licenza Apache-2.0 (`LICENSE`, copiata in `worker/` per il wheel) e metadati del pacchetto: README breve per PyPI, URL del progetto, versione `0.1.0`.
- CI (`.github/workflows/ci.yml`): ruff, mypy, suite di test su pgvector per Postgres 15, 16, 17 e 18 (tutti verdi in locale), `make test-extension`. Rilascio (`release.yml`) su tag `v*` che deve coincidere con la versione del pacchetto: wheel e sdist su PyPI con trusted publishing, immagine del worker e immagini Postgres con l'estensione (pg17, pg18) su GHCR, file dell'estensione allegati alla release GitHub. L'immagine Postgres prende `PG_MAJOR`.
- Avvio rapido in un comando: `compose.yml` alza Postgres con pgvector, esegue `pgbee install` una volta e avvia il worker; `examples/quickstart.sql` dichiara una colonna `decision`, una `llm` e una `embedding` su otto ticket (riempite in 6 s, 0.0005 USD). La chiave è opzionale per compose, così `psql` funziona anche senza, e il worker esce con un messaggio chiaro. Verificato da un clone pulito. Log senza colori fuori da un terminale.
- README riscritto per chi non conosce il progetto: cosa fa nelle prime righe, avvio rapido, esito della prova sul campo, riferimento SQL completo (funzioni con i nomi dei parametri `p_`, impostazioni, viste, CLI del worker), modi di installare, caricamenti massivi, limiti e sicurezza. `SECURITY.md` con il canale di segnalazione privata e il modello di sicurezza.
- `sql/0012`: il commento di `bee.configure` elenca tutte le impostazioni, quello che mostra `\df+`.
- `ARCHITECTURE.md` tradotta in inglese e allineata a prova sul campo, avvio rapido e Postgres 15-18; `CONVENTIONS.md` fissa quali file sono in inglese (per chi usa il progetto) e quali restano note di lavoro in italiano.

**Decisioni.** #21 (Apache-2.0, progetto personale su `essedev`); #9 aggiornata con la divisione delle lingue.

**Prossimo passo.** I passi di pubblicazione che restano all'autore, in `ROADMAP.md`: repo GitHub, push, publisher PyPI, segnalazioni private, tag `v0.1.0`, prova su un Postgres gestito.

## Ciclo 9 (27 settembre 2026): pronto per il lancio

**Obiettivo.** Chiudere la milestone prima del lancio: dimostrare perché lo stato sta nel database, aprire il worker a provider diversi da OpenRouter, servire chi non può tenere un processo acceso, rendere ripetibili le misure.

**Fatto.**

- Prova sotto guasti in `bench/failure/`: lo stesso carico (2000 righe, inserimenti, modifiche dall'applicazione e da uno script che la scavalca, un cambio di prompt, correzioni umane sul 3 per cento) su pgbee e su due progetti nel codice applicativo con coda durevole a lease, "salva, poi accoda" e outbox transazionale; SIGKILL di applicazione o worker ogni ~60 operazioni, due interruzioni della coda esterna, modello finto deterministico con il 3 per cento di 5xx, `pgbee run` vero. Su tre seed pgbee chiude a zero; gli altri perdono 20-30 correzioni su 56-60 e lasciano 15-49 valori vecchi (numeri e letture in `bench/README.md` e `ANALYSIS.md`). Una correzione umana che scrive il valore già presente non è un override (gli ORM riscrivono tutte le colonne): lo scenario usa correzioni che cambiano il valore, il README lo dichiara fra i limiti.
- Deadlock trovato dalla prova e corretto nello `sql/0014`: l'UPDATE dell'applicazione blocca la riga e poi, col trigger, il job; `complete_job` bloccava il job e poi la riga, e Postgres uccideva una delle due transazioni, a volte quella dell'utente. Ora `complete_job` blocca prima la riga (`FOR NO KEY UPDATE`), poi il job, e ricontrolla; un test lo riproduce con due connessioni. Il worker non esce più quando il database rifiuta un risultato (CHECK o trigger dell'utente, conflitto di lock): il job fallisce, ritentabile se transitorio; esce solo se perde la connessione.
- Provider compatibile OpenAI (OpenAI, Azure v1, Ollama, vLLM) accanto a OpenRouter, uno per processo, scelto dall'ambiente (`PGBEE_PROVIDER`, `OPENAI_API_KEY`, `OPENAI_BASE_URL`): solo parametri standard col loro nome, `llm` ed `embedding`, `decision` resta su OpenRouter e `pgbee run` rifiuta di servirlo altrimenti. Prezzi per milione di token nella config della colonna (`input_usd_per_mtok`, `output_usd_per_mtok`, validati dallo `sql/0013`): il worker calcola il costo quando il provider non lo riporta, e spesa, tetti e viste funzionano. `PGBEE_REQUEST_TIMEOUT_SECONDS` per i server locali lenti. Provato su Ollama in Docker (qwen2.5:0.5b, all-minilm): valori, vettori e costo dai prezzi; un modello senza ragionamento rifiuta `reasoning_effort` con un 400 e il job muore senza ritentare, come deve. Ollama in Docker su Mac si blocca con tutti i thread della VM occupati: con `num_thread 4` risponde in 5 s.
- `pgbee run --drain [--max-seconds N]`: le stesse corsie senza attese, fino a coda vuota, poi uscita; prima restituisce i job delle esecuzioni morte. Sezione nel README per crontab, CronJob e job schedulati. Provato come processo vero contro Ollama.
- Benchmark nel repository: `bench/failure/`, `bench/scale.py` (dichiarazione, caricamenti massivi e claim su 1M righe, riscrive gli script rimasti fuori dal repo), `bench/lanes.py` (ciclo unico contro una corsia per backend con latenze simulate), risultati in `bench/results/`, target `make bench-*`, lint e typecheck anche in CI.

**Decisioni.** #22 (provider compatibile OpenAI, uno per worker, prezzi nella config), #23 (modalità drain).

**Prossimo passo.** La milestone di pubblicazione in `ROADMAP.md`: passi di Simone su PyPI e GitHub, prova su un Postgres gestito, repository pubblico e tag su comando.

## Ciclo 10 (27 settembre 2026): changelog e prova su Postgres gestito

**Obiettivo.** Rilasci con note scritte per chi usa il progetto, e la prima prova fuori da Postgres in Docker.

**Fatto.**

- `CHANGELOG.md` in formato Keep a Changelog con il contenuto della 0.1.0 in `Unreleased`; `.github/scripts/changelog.py` (solo libreria standard) lo valida in CI, estrae la sezione di una versione e trasforma `Unreleased` in rilascio; `release.yml` usa quella sezione come testo della release GitHub e si ferma prima di pubblicare se manca (le note generate da GitHub, costruite dalle pull request, sarebbero uscite vuote). `make changelog-release`, `make release-notes`, regole e procedura in `CONVENTIONS.md`, `actionlint` pulito.
- Neon, PostgreSQL 18.6, ruolo proprietario con `CREATEROLE` ma senza superuser: la suite completa passa (96 test) e `pgbee run` vero riempie le righe con un modello finto locale, `--drain` compreso. Dall'INSERT al valore 0.3 s sull'host diretto, 3.9 s attraverso il pooler `-pooler`: PgBouncer in modalità transazione perde i `NOTIFY` e il worker trova i job al giro di polling; nient'altro si rompe, prepared statement compresi. README: il worker usa l'host diretto, l'applicazione può restare sul pooler.
- Tre difetti dei test emersi su Neon, nessuno di pgbee: una password di prova troppo debole per la policy di Neon, `DROP OWNED BY` su un ruolo di prova che senza superuser richiede di assegnarsi prima il ruolo (Postgres 16+), un test di manutenzione che invecchiava i job prima che finissero tutti (la latenza di rete ha allargato la finestra).
- Database di Neon ripulito a fine prova: schema `bee`, ruolo `bee_worker`, database dei test.
- Pubblicazione: controllo della storia prima di aprire (un solo autore con email personale, nessun segreto, nessun `.env`), repository pubblico con segnalazioni private di vulnerabilità, environment `pypi` con revisore obbligatorio e solo tag `v*`, rilascio `v0.1.0` approvato a mano: PyPI, immagini GHCR pubbliche, release GitHub dal changelog.

**Decisioni.** #24 (changelog scritto a mano come testo della release).

**Prossimo passo.** Pubblicazione fatta nel ciclo stesso: resta l'anteprima social del repository (Simone), poi la presentazione pubblica del progetto.

## Ciclo 11 (27 settembre 2026): presentazione pubblica dopo la 0.1.0

**Obiettivo.** Rifinire ciò che vede chi arriva sul repository ora che è pubblico: logo, README, e separare i piani dell'autore dalla doc del progetto.

**Fatto.**

- Palette C1 per il logo chiaro e scuro (`assets/brand/build.py`): nel chiaro contorno e "bee" `#3F2818`, "pg" `#A0714A`; nello scuro contorno `#6E4726`, "pg" `#A0714A`, "bee" `#FFC21A`. Il corpo color miele resta l'unico colore acceso. `assets/brand/logo-colors.html` conserva le palette confrontate; PNG rigenerati.
- README centrato su ciò che pgbee garantisce: via la riga "nobody runs it in production"; la prova sul campo riporta job, fallimenti, tracciamento dei costi e coda di revisione, mentre la tabella per modello (costo, latenza, accordo) passa in `demo/cfpb/README.md` con i comandi per rifarla, perché prezzi e latenze sono dei provider e invecchiano.
- Piani di lancio, piano di validazione e offerta fuori dalla doc pubblica, in una cartella ignorata da git (#25): `ROADMAP.md` perde la milestone di lancio e il piano di validazione, `ANALYSIS.md` e `CYCLES.md` non li citano più, `ANALYSIS.md` toglie due giudizi sbrigativi su altri progetti.
- Video di presentazione di 53 s sotto la tagline del README, come allegato GitHub (`user-attachments`): è l'unico modo in cui GitHub mostra un player in un README, un mp4 nel repository appariva come un link a una pagina che non lo riproduce. File e poster tolti dall'albero; sorgente e render nel repo motion-studio (`src/videos/pgbee-cells/`).

**Decisioni.** #25 (piani dell'autore fuori dal repository pubblico).

**Prossimo passo.** Anteprima social del repository (Simone); M4 (demo UI) solo se la demo convince.

## Ciclo 12 (30 settembre 2026): `halfvec` e rilascio 0.2.0

**Obiettivo.** Primo uso di pgbee dentro un'applicazione vera, come coda unica per ingestion ed embedding: gli embedding vanno su `halfvec(1024)` con indice HNSW, e pgbee sapeva scrivere solo `vector`.

**Fatto.**

- Tipo di output `halfvec` nello 0015: `add_column` crea o accetta una colonna `halfvec(N)`, `complete_job` fa il cast, `update_column` blocca il cambio di dimensioni come per `vector`, `_validate_definition` richiede pgvector 0.7 o successivo. Il contratto del worker non cambia.
- Il valore enum aggiunto con `ALTER TYPE ... ADD VALUE` non si può usare nella transazione che lo crea, e sia `pgbee install` sia `ALTER EXTENSION pgbee UPDATE` applicano un file per transazione: le funzioni SQL ridefinite nello stesso file confrontano `output_type::text`, perché il loro corpo viene analizzato alla creazione.
- Test: scrittura, dimensioni sbagliate, colonna già esistente, rifiuto con `llm` e di un tipo diverso da un vettore con `embedding`, worker vero con provider finto su una colonna `halfvec`, e nella catena di update dell'estensione gli stessi valori enum di un'installazione nuova più un `add_column` `halfvec` dopo l'update. 99 test più 3 di estensione.
- Rilascio 0.2.0: estensione 0.15, changelog con la sezione Upgrading.

**Decisioni.** #26 (`halfvec` come tipo di output a sé).

**Prossimo passo.** Raccogliere gli altri attriti dell'integrazione (installazione prima delle migrazioni dell'applicazione, test dell'applicazione che fanno da worker, backend `custom` per l'OCR) e decidere quali diventano prodotto.

## Ciclo 13 (30 settembre 2026): righe cancellate, vettori e rilascio 0.3.0

**Obiettivo.** Chiudere i difetti emersi dal primo uso in un'applicazione (ingestion di documenti: un job `custom` scrive il markdown e sostituisce i chunk di una tabella collegata, la colonna `embedding` dei chunk è derivata).

**Fatto.**

- Trigger `bee_forget_<colonna>` e `bee_forget_all_<colonna>` (0016): una riga cancellata o una tabella troncata esce dal lineage corrente e perde i job vivi. Correggono due difetti: i chunk riscritti a ogni reingestione lasciavano risultati correnti orfani che `bee.prune` non toccava, e una riga cancellata e reinserita con stessa chiave e stesse sorgenti non veniva mai calcolata. L'aggiornamento ritira gli orfani esistenti con un anti-join per tabella.
- I risultati delle colonne `vector` e `halfvec` non copiano più il vettore in `bee.result.value`: in jsonb pesava dieci volte la colonna.
- `bee.definition(tabella, colonna)`: la definizione corrente senza i contatori della coda, per l'applicazione che deve incorporare le query con lo stesso modello della colonna a ogni richiesta.
- README: sezione su come testare un'applicazione che usa pgbee facendo da worker con `claim_jobs` e `complete_job`, la stessa ricetta con cui l'applicazione testa l'ingestion.
- Prova sotto guasti rilanciata dopo la ridefinizione di `complete_job`: pgbee a zero su righe senza valore, valori vecchi e correzioni sovrascritte (seed 7, 47 processi uccisi). Il file di risultati citato nel README non è stato sostituito.
- Rilascio 0.3.0: estensione 0.16. 105 test più 4 di estensione, tra cui l'aggiornamento dalla 0.15 con orfani e vettori già salvati.

**Decisioni.** #27 (righe cancellate fuori dal lineage corrente), #28 (vettori fuori dal lineage).

**Prossimo passo.** L'applicazione passa a `bee.definition` al posto della lettura delle tabelle interne; la destinazione a tabella per il chunking resta rinviata finché non arriva un secondo caso.
