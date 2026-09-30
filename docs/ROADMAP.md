# Roadmap

## Stato corrente

Estensione, worker e demo girano end to end contro OpenRouter o un endpoint compatibile OpenAI, con quattro backend (`llm`, `decision`, `embedding`, `custom`), tetto di spesa per colonna, backfill a chunk che regge tabelle da milioni di righe, worker con privilegi minimi, retention del lineage, packaging (wheel con i file SQL, `CREATE EXTENSION pgbee`, immagini Docker) e un ciclo di lavoro per backend. Provati sul campo su 3000 reclami reali CFPB: 12.000 job senza errori per 0.41 USD. Una review esterna ha trovato un bug di lineage sui cambi di versione, corretto nello 0011. Licenza Apache-2.0, CI verde su Postgres 15-18, rilascio automatico su tag, avvio rapido con `docker compose up`, README e `SECURITY.md` per chi arriva da fuori, logo e mascotte in `assets/brand/`. Prova sotto guasti contro due progetti nel codice applicativo in `bench/failure/` (ha trovato un deadlock fra applicazione e `complete_job`, corretto nello 0014), provider compatibile OpenAI con prezzi per token (provato su Ollama), `pgbee run --drain` per cron e serverless, benchmark ripetibili in `bench/`, prova su Neon. Repository `essedev/pgbee` pubblico, 0.1.0 su PyPI e GHCR dal 27 settembre 2026 (ciclo 10), README con video di presentazione di 53 s. 0.2.0 con il tipo di output `halfvec` (ciclo 12) e 0.3.0 con righe cancellate fuori dal lineage corrente, vettori fuori dal lineage e `bee.definition` (ciclo 13), nate dal primo uso di pgbee dentro un'applicazione. Cicli 1-13 in `CYCLES.md`, risultati in `ANALYSIS.md`, `bench/README.md` e `demo/cfpb/README.md`.

## Milestone aperte

- [ ] Anteprima social del repository: `assets/brand/png/pgbee-social.png` in Settings, Social preview (Simone). Unico passo rimasto della pubblicazione 0.1.0.
- [ ] M4 Demo UI: piccola app che mostra la tabella e le celle che si riempiono, la coda di revisione e il lineage di una cella. Solo se la demo convince. Porte riservate: API 4461, web 4462.

## Più avanti

Worker ospitato (ci si collega il proprio database e il worker gira come servizio: risolve chi non può tenere un processo acceso), few-shot dagli override umani, chunking con destinazione a tabella (per `DECISIONS.md` #11: il primo uso reale scrive i chunk lato applicazione nella transazione di un job `custom`, e funziona; si riapre con un secondo caso), pubblicazione su PGXN o `pg_tle` (per `DECISIONS.md` #18), worker in Rust (per `DECISIONS.md` #3: solo se i primi utenti chiedono un eseguibile senza Python; non renderebbe il worker più veloce, perché aspetta i modelli), estensione SQLite con lo stesso modello concettuale.
