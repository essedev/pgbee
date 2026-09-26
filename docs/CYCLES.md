# Cicli

Un ciclo è un'unità di lavoro chiusa. Il file tiene gli ultimi ~15; i più vecchi vanno in `docs/cycles-archive/`. Le decisioni durevoli stanno in `DECISIONS.md`, le milestone aperte in `ROADMAP.md`.

## Ciclo 1 (26 settembre 2026): dall'idea alla demo end to end

**Obiettivo.** Verificare in una giornata se il modello "colonna derivata mantenuta dal database" regge un ciclo completo su dati realistici, prima di decidere se farne un prodotto.

**Fatto.**

- Analisi di mercato e concorrenza (`ANALYSIS.md`), overview HTML con pro e contro delle decisioni (istantanea archiviata in `archive/overview-2026-09-26.html`, non più mantenuta).
- Estensione SQL (`sql/0001`-`0003`): schema `ai` con catalogo, versioni, coda, lineage, trigger di accodamento e override, contratto worker, viste. `0002` corregge il tipo della vista dei costi, `0003` azzera tentativi e backoff dei job pending quando nasce una versione nuova.
- Worker di riferimento `aicol` (install, run, status): `LISTEN` più poll, batch per backend, backend `llm` con output strutturato, `embedding` a lotti, classificazione degli errori, parse JSON tollerante, drain consapevole del backoff. 27 test di integrazione sull'estensione senza modelli, 11 sul worker con provider finto più uno marcato `llm`.
- Demo ticket di assistenza: 21 ticket, sei colonne derivate, otto passi che coprono coda, ticket nuovo, override che resta, cambio prompt con ricalcolo selettivo, ricerca dei simili via embedding, lineage e costi. Stima di spesa con conferma prima di chiamare i modelli.
- Confronto di otto modelli su `urgency` e `category` contro `gold.json` (`compare_models.py`, tabella in `ANALYSIS.md`): i modelli piccoli del 2026 sono equivalenti a Haiku 4.5 a un decimo del costo; default della demo spostato a `openai/gpt-6-luna` con effort low.
- Backend `decision` (`sql/0004`) per i modelli a risposta tipizzata: TypeSafe Jev via `POST /api/alpha/decisions` di OpenRouter, probabilità calibrate in `ai.result.details`, `complete_job` con parametro `p_details` compatibile. Sui ticket: 100 per cento, 28 volte meno costoso di Haiku 4.5, confidenza che scende davvero sui casi ambigui.

**Decisioni.** #1-#12 in `DECISIONS.md`, tutte prese in questo ciclo; #12 (backend `decision`) estende il perimetro di #10.

**Prossimo passo.** Valutare il criterio di uscita in `ANALYSIS.md` e decidere se fare M4 (demo UI). Aperto: `.env.example` non elenca `OPENROUTER_BASE_URL`, letta dal worker.

## Ciclo 2 (26 settembre 2026): tetto di spesa, chiamate Jev condivise, worker più solido

**Obiettivo.** Chiudere i punti deboli emersi dalla demo prima di valutare il criterio di uscita: spesa senza limite per colonna, domande `decision` pagate una chiamata per colonna, worker lento a svegliarsi e a fermarsi.

**Fatto.**

- Tetto di spesa per colonna (`sql/0005`): `budget_usd` e `budget_period` (`day`, `month`, `total`) nella config, tabella `ai.spend` alimentata da un trigger su `ai.result`, vista `ai.budgets`, funzione `ai.spent`. `ai.claim_jobs` salta le colonne esaurite, `ai.configure` sveglia i worker quando il tetto sale. Speso e residuo in `aicol status`; la demo mette un tetto su `urgency`.
- Colonne `decision` della stessa riga e dello stesso modello in una chiamata Jev sola, fino a 16 domande: tre domande 0.0000199 USD contro 0.0000406 in tre chiamate. Usage ripartito fra i job, `questions_in_call` nei `details`, una risposta mancante fa fallire solo il suo job.
- `ai.claim_jobs` porta con sé i job `decision` pronti delle stesse righe (`sql/0006`), così la condivisione vale anche nel backfill; un claim può restituire più di `batch_size` job, nessuna firma cambia.
- Worker: le notifiche arrivate durante un batch si consumano tutte al risveglio (prima N insert davano N claim vuoti); `stop()` interrompe subito poll e pausa da rate limit.
- Test: provider finto e helper spostati in `tests/fakes.py`; test del demone (risveglio, backlog, stop, reclaim) e del budget; smoke test contro OpenRouter per `llm`, `decision` ed `embedding`, marcati `llm` (costo sotto 0.001 USD). `.env.example` ora elenca `OPENROUTER_BASE_URL`.

**Decisioni.** #13 (tetto di spesa contato nel database e applicato al claim), #14 (fratelli `decision` nello stesso claim).

**Prossimo passo.** Invariato dal ciclo 1: valutare il criterio di uscita in `ANALYSIS.md` e decidere se fare M4 (demo UI).

## Ciclo 3 (26 settembre 2026): backfill a chunk

**Obiettivo.** Togliere il limite dichiarato nei tradeoff: il backfill accodava tutta la tabella in una transazione, con la tabella utente bloccata in scrittura per l'intera scansione e un job per riga in coda.

**Fatto.**

- Backfill incrementale (`sql/0007`): `ai.backfill` accoda un chunk in ordine di chiave primaria (`backfill_chunk` nella config, default 1000, validato fra 1 e 100000) e salva un cursore in `column_def` (`backfill_pending`, `backfill_cursor`, `backfill_scanned`); `ai.claim_jobs` accoda i chunk successivi prima e dopo il claim finché la coda della colonna ha meno di un chunk pending (al massimo dieci per claim) e manda una `NOTIFY` finché la scansione non è finita. Colonne disabilitate o oltre budget non avanzano. Nessuna firma del contratto cambia.
- Su 1M righe `add_column` passa da 39 s con la tabella bloccata a 0.06 s.
- `ai.columns` e `aicol status` mostrano lo stato della scansione.
- Test: sei sull'estensione (chunk e claim, righe già a posto saltate, chiavi composte e uuid, `update_column` a chunk con righe pinnate, colonne ferme, validazione) e uno sul demone, che completa un backfill a chunk senza aspettare il poll.

**Decisioni.** #15 (backfill a chunk con cursore, guidato da `claim_jobs`).

**Prossimo passo.** Invariato: valutare il criterio di uscita in `ANALYSIS.md` e decidere se fare M4 (demo UI).

## Ciclo 4 (26 settembre 2026): caricamenti massivi, privilegi minimi, retention del lineage

**Obiettivo.** Rendere l'estensione installabile in un database vero: misurare i caricamenti massivi, togliere al worker l'accesso alle tabelle utente, fermare la crescita senza limite di `ai.result` e `ai.job`.

**Fatto.**

- Caricamento massivo misurato su 1M righe (solo doc, tradeoff in `ARCHITECTURE.md`): 38 s con una colonna derivata attiva contro 3.7 s senza, trigger per statement misurato a 33 s e scartato; la ricetta è `ai.disable`, caricamento, `ai.enable` (6.7 s più 19 ms). Documentato anche il claim lento (1-3 s) finché l'autovacuum non aggiorna le statistiche di `ai.job`.
- Ruolo `ai_worker` (`sql/0008`): funzioni del contratto e trigger di accodamento e override diventano `SECURITY DEFINER` con `search_path` fissato, `EXECUTE` sul contratto revocato a `PUBLIC` e concesso al ruolo, `SELECT` sulle viste. Il worker non ha grant sulle tabelle utente; un ruolo applicativo che scrive solo la tabella accoda e registra override. Cast a `vector` qualificato con lo schema. Regola sulle ridefinizioni nella rule SQL.
- Retention del lineage (`sql/0009`): `lineage_retention_days` nella config (default null, conservare tutto), `ai.prune(limit, jobs_older_than)` cancella a lotti i risultati non correnti scaduti e i job `done` oltre una settimana, mai risultati correnti, job `dead` o `ai.spend`. Entra nel contratto worker; il worker di riferimento la chiama all'avvio e ogni `AICOL_MAINTENANCE_INTERVAL_SECONDS` (default un'ora), un errore di manutenzione non ferma la coda.
- Test: quattro sui ruoli (`test_roles.py`, ruoli di cluster creati e rimossi dal modulo), cinque sulla retention, due sul demone (manutenzione all'avvio e a intervallo, errore che non ferma la coda).

**Decisioni.** #16 (privilegi minimi con funzioni definer), #17 (retention per colonna eseguita dal worker).

**Prossimo passo.** Invariato: valutare il criterio di uscita in `ANALYSIS.md` e decidere se fare M4 (demo UI).

## Ciclo 5 (26 settembre 2026): packaging, `CREATE EXTENSION`, immagine del worker

**Obiettivo.** Rendere l'estensione installabile fuori dal repository: sui Postgres gestiti con il worker, sui server self-hosted come estensione vera, e il worker eseguibile in un container.

**Fatto.**

- I file SQL passano in `worker/src/aicol/sql/` (`sql` alla radice resta come link simbolico) e viaggiano nel wheel: `aicol install` funziona fuori dal repository, `make build` produce wheel e sdist.
- `aicol extension-files` genera `aicol.control`, `aicol--0.1.sql` e uno script di update per ogni file successivo, ciascuno con `pg_extension_config_dump` sulle tabelle di `ai` così `pg_dump` ne salva i dati. `make extension-image` costruisce l'immagine pgvector con i file, `make test-extension` prova create, catena di update da 0.1 e dump e restore su un container usa e getta (porta 4463). `aicol install` rifiuta un database che ha già l'estensione.
- Il giro dump e restore ha trovato due difetti invisibili con `aicol install`, corretti in `sql/0010`: la FK circolare fra `column_def` e `column_version`, e il trigger su `ai.result` che contava la spesa, ora dentro `ai.complete_job`. Regola nuova nella rule SQL: niente trigger fra tabelle di `ai`, niente FK circolari.
- Immagine Docker del worker (`make worker-image`): multi stage sull'immagine di uv, utente non root, `aicol run` di default, configurazione solo da ambiente, `.env` esclusi dal build context.
- Test: quattro in `test_packaging.py` (catena dei file generati più tre marcati `extension`), uno sull'estensione che verifica che la versione corrente appartenga sempre alla sua colonna.

**Decisioni.** #18 (due modi di installare generati dagli stessi file); #1 aggiornata di conseguenza, #13 aggiornata sul punto in cui si conta la spesa.

**Prossimo passo.** Invariato: valutare il criterio di uscita in `ANALYSIS.md` e decidere se fare M4 (demo UI).
