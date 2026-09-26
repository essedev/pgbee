# Roadmap

## Stato corrente

Estensione, worker e demo girano end to end contro OpenRouter, con quattro backend (`llm`, `decision`, `embedding`, `custom`), tetto di spesa per colonna, backfill a chunk che regge tabelle da milioni di righe, worker con privilegi minimi, retention del lineage, otto modelli confrontati, packaging (wheel con i file SQL, `CREATE EXTENSION pgbee` per i server self-hosted, immagine Docker del worker) e un ciclo di lavoro per backend. Provati sul campo su 3000 reclami reali CFPB: 12.000 job senza errori per 0.41 USD. Una review esterna ha trovato un bug di lineage sui cambi di versione, corretto nello 0011. Il repository è pronto per uscire: licenza Apache-2.0, CI su Postgres 15-18, rilascio automatico su tag, avvio rapido con `docker compose up`, README e `SECURITY.md` per chi arriva da fuori (cicli 1-8 in `CYCLES.md`, risultati in `ANALYSIS.md`).

## Milestone corrente: pubblicazione 0.1.0

Il codice è pronto; restano i passi che richiedono gli account dell'autore, in quest'ordine.

- [ ] Repository pubblico `essedev/pgbee` su GitHub (per `DECISIONS.md` #21), remote `origin` (oggi il repository locale non ne ha) e push di `main`; controllare che la CI passi su tutte e quattro le versioni di Postgres.
- [ ] Trusted publisher su PyPI per il progetto `pgbee`: repository `essedev/pgbee`, workflow `release.yml`, environment `pypi`.
- [ ] Segnalazioni private attive nelle impostazioni di sicurezza del repository: `SECURITY.md` rimanda lì.
- [ ] Tag `v0.1.0` (deve coincidere con la versione in `worker/pyproject.toml`, il workflow lo verifica): pubblica wheel su PyPI, immagini su GHCR e file dell'estensione nella release. Dopo il primo rilascio verificare che i package GHCR `pgbee-worker` e `pgbee-postgres` siano pubblici e collegati al repository: i Dockerfile non hanno la label `org.opencontainers.image.source`.
- [ ] Prova su un Postgres gestito (Supabase, Neon o RDS) con `pgbee install` dal pacchetto pubblicato: il README dichiara che non è ancora stato fatto.

## Dopo la pubblicazione: piano di validazione (proposto, in attesa di verdetto)

Piano in quattro settimane emerso dalla review esterna, al posto della valutazione del criterio di uscita in `ANALYSIS.md` fatta solo sui dati propri. Non ratificato: se Simone lo approva, i criteri sostituiscono quel criterio di uscita.

- [ ] Interviste a 8 responsabili tecnici, di cui 4 esterni.
- [ ] Due installazioni reali.
- [ ] Prove sotto guasto.
- [ ] Un pilota a pagamento.

Criteri di esito: **prodotto** se due gruppi esterni lo usano da soli e uno paga; **strumento interno** se risparmia lavoro solo nei progetti di consulenza dell'autore; altrimenti **chiusura**, e resta il modello concettuale.

## Milestone aperte

- [ ] M4 Demo UI: piccola app che mostra la tabella e le celle che si riempiono, la coda di revisione e il lineage di una cella. Solo se la demo convince. Porte riservate: API 4461, web 4462.

## Dopo l'esperimento (solo se passa)

Few-shot dagli override umani, chunking con destinazione a tabella (per `DECISIONS.md` #11), pubblicazione su PGXN o `pg_tle` (per `DECISIONS.md` #18), worker in Rust (per `DECISIONS.md` #3: solo se i primi utenti chiedono un eseguibile senza Python; non renderebbe il worker più veloce, perché aspetta i modelli), estensione SQLite con lo stesso modello concettuale.
