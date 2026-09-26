# Roadmap

## Stato corrente

Estensione, worker e demo girano end to end contro OpenRouter, con quattro backend (`llm`, `decision`, `embedding`, `custom`) e otto modelli confrontati (ciclo 1 in `CYCLES.md`, risultati in `ANALYSIS.md`). Il criterio di uscita dell'esperimento è in `ANALYSIS.md`: il prossimo passo è valutarlo.

## Milestone aperte

- [ ] M4 Demo UI: piccola app che mostra la tabella e le celle che si riempiono, la coda di revisione e il lineage di una cella. Solo se la demo convince. Porte riservate: API 4461, web 4462.

## Dopo l'esperimento (solo se passa)

Più domande `decision` sulla stessa riga in una chiamata sola (Jev risponde a molte domande sullo stesso testo quasi allo stesso costo), few-shot dagli override umani, chunking con destinazione a tabella (per `DECISIONS.md` #11), backfill a lotti per tabelle grandi, budget per definizione, packaging come estensione installabile, worker in Rust (per `DECISIONS.md` #3), estensione SQLite con lo stesso modello concettuale.
