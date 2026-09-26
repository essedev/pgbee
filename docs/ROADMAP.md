# Roadmap

## Stato corrente

Estensione, worker e demo girano end to end contro OpenRouter, con quattro backend (`llm`, `decision`, `embedding`, `custom`), tetto di spesa per colonna, backfill a chunk che regge tabelle da milioni di righe e otto modelli confrontati (cicli 1-3 in `CYCLES.md`, risultati in `ANALYSIS.md`). Il criterio di uscita dell'esperimento è in `ANALYSIS.md`: il prossimo passo è valutarlo.

## Milestone aperte

- [ ] M4 Demo UI: piccola app che mostra la tabella e le celle che si riempiono, la coda di revisione e il lineage di una cella. Solo se la demo convince. Porte riservate: API 4461, web 4462.

## Dopo l'esperimento (solo se passa)

Few-shot dagli override umani, chunking con destinazione a tabella (per `DECISIONS.md` #11), packaging come estensione installabile, worker in Rust (per `DECISIONS.md` #3), estensione SQLite con lo stesso modello concettuale.
