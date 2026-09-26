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

**Decisioni.** Nessuna nuova: lo 0011 ripristina l'invariante di #4 (ogni risultato riferisce la versione che l'ha prodotto). La review ha proposto anche un piano di validazione in quattro settimane, registrato in `ROADMAP.md` come proposta in attesa di verdetto.

**Prossimo passo.** Decidere sul piano di validazione; se passa, sostituisce la valutazione del criterio di uscita in `ANALYSIS.md` fatta solo sui dati propri.
