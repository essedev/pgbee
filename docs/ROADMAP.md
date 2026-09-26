# Roadmap

## Stato corrente

Estensione, worker e demo girano end to end contro OpenRouter, con quattro backend (`llm`, `decision`, `embedding`, `custom`), tetto di spesa per colonna, backfill a chunk che regge tabelle da milioni di righe, worker con privilegi minimi, retention del lineage, otto modelli confrontati e packaging (wheel con i file SQL, `CREATE EXTENSION aicol` per i server self-hosted, immagine Docker del worker; cicli 1-5 in `CYCLES.md`, risultati in `ANALYSIS.md`). Il criterio di uscita dell'esperimento è in `ANALYSIS.md`: il prossimo passo è valutarlo.

## Milestone aperte

- [ ] M4 Demo UI: piccola app che mostra la tabella e le celle che si riempiono, la coda di revisione e il lineage di una cella. Solo se la demo convince. Porte riservate: API 4461, web 4462.

## Dopo l'esperimento (solo se passa)

Few-shot dagli override umani, chunking con destinazione a tabella (per `DECISIONS.md` #11), pubblicazione su PGXN o `pg_tle` (per `DECISIONS.md` #18), worker in Rust (per `DECISIONS.md` #3), estensione SQLite con lo stesso modello concettuale.
