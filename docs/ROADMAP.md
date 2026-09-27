# Roadmap

## Stato corrente

Estensione, worker e demo girano end to end contro OpenRouter o un endpoint compatibile OpenAI, con quattro backend (`llm`, `decision`, `embedding`, `custom`), tetto di spesa per colonna, backfill a chunk che regge tabelle da milioni di righe, worker con privilegi minimi, retention del lineage, packaging (wheel con i file SQL, `CREATE EXTENSION pgbee`, immagini Docker) e un ciclo di lavoro per backend. Provati sul campo su 3000 reclami reali CFPB: 12.000 job senza errori per 0.41 USD. Una review esterna ha trovato un bug di lineage sui cambi di versione, corretto nello 0011. Licenza Apache-2.0, CI verde su Postgres 15-18, rilascio automatico su tag, avvio rapido con `docker compose up`, README e `SECURITY.md` per chi arriva da fuori, logo e mascotte in `assets/brand/`. Il repository `essedev/pgbee` esiste su GitHub, privato. Pronto per il lancio (ciclo 9): prova sotto guasti contro due progetti nel codice applicativo in `bench/failure/` (ha trovato un deadlock fra applicazione e `complete_job`, corretto nello 0014), provider compatibile OpenAI con prezzi per token (provato su Ollama), `pgbee run --drain` per cron e serverless, benchmark ripetibili in `bench/`. Cicli 1-9 in `CYCLES.md`, risultati in `ANALYSIS.md` e `bench/README.md`.

## Milestone corrente: pubblicazione 0.1.0

- [ ] Trusted publisher su PyPI per il progetto `pgbee`: repository `essedev/pgbee`, workflow `release.yml`, environment `pypi` (Simone, richiede il suo account).
- [ ] Anteprima social del repository: `assets/brand/png/pgbee-social.png` in Settings, Social preview (Simone).
- [ ] Prova su un Postgres gestito (Neon, Supabase o RDS) con `pgbee install`: serve la stringa di connessione di un database di prova. Il README dichiara che non è ancora stato fatto.
- [ ] Repository pubblico e segnalazioni private di vulnerabilità attive (`SECURITY.md` rimanda lì), su comando di Simone.
- [ ] Rilascio `v0.1.0` su comando: `make changelog-release version=0.1.0`, commit `chore(release): 0.1.0`, tag `v0.1.0` (deve coincidere con la versione in `worker/pyproject.toml` e avere la sua sezione in `CHANGELOG.md`, il workflow verifica entrambe). Il tag pubblica wheel su PyPI, immagini su GHCR e file dell'estensione nella release. Dopo il primo rilascio verificare che i package GHCR siano pubblici e collegati al repository.
- [ ] Video di 30 secondi delle celle che si riempiono, per README e post.
- [ ] Lancio: profilo X e avatar (`assets/brand/png/pgbee-avatar-512.png`); bozze dei post in inglese scritte dall'agent e approvate una per una da Simone; lo stesso materiale su LinkedIn, Hacker News (Show HN) e r/PostgreSQL. Apify per trovare chi discute già il problema (candidati alle interviste), Zernio solo per programmare post già approvati, niente risposte automatiche.

## Dopo la pubblicazione: piano di validazione (proposto, in attesa di verdetto)

Piano in quattro settimane emerso dalla review esterna, al posto della valutazione del criterio di uscita in `ANALYSIS.md` fatta solo sui dati propri. Non ratificato: se Simone lo approva, i criteri sostituiscono quel criterio di uscita.

- [ ] Interviste a 8 responsabili tecnici, di cui 4 esterni.
- [ ] Due installazioni reali.
- [ ] Prove sotto guasto su un'installazione vera.
- [ ] Un pilota a pagamento.

Criteri di esito: **prodotto** se due gruppi esterni lo usano da soli e uno paga; **strumento interno** se risparmia lavoro solo nei progetti di consulenza dell'autore; altrimenti **chiusura**, e resta il modello concettuale.

## Milestone aperte

- [ ] M4 Demo UI: piccola app che mostra la tabella e le celle che si riempiono, la coda di revisione e il lineage di una cella. Solo se la demo convince. Porte riservate: API 4461, web 4462.

## Dopo l'esperimento (solo se passa)

Worker ospitato (ci si collega il proprio database e il worker gira come servizio: risolve chi non può tenere un processo acceso ed è l'offerta commerciale più naturale), few-shot dagli override umani, chunking con destinazione a tabella (per `DECISIONS.md` #11), pubblicazione su PGXN o `pg_tle` (per `DECISIONS.md` #18), worker in Rust (per `DECISIONS.md` #3: solo se i primi utenti chiedono un eseguibile senza Python; non renderebbe il worker più veloce, perché aspetta i modelli), estensione SQLite con lo stesso modello concettuale.
