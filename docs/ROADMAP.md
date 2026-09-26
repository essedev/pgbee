# Roadmap

## Stato corrente

M1 e M2 completate: estensione SQL e worker di riferimento, con test di integrazione. M3 scritta, da eseguire con una chiave OpenRouter. Il criterio di uscita dell'esperimento è in `ANALYSIS.md`.

## Prossime milestone

- [x] M1 Estensione: schema `ai`, `add_column`/`update_column`/`drop_column` con backend `llm`, `embedding` e `custom`, trigger di accodamento e override, contratto worker (`claim_jobs`, `complete_job`, `fail_job`, `reclaim_stale`), viste. Test di integrazione pytest su Postgres in Docker che esercitano trigger, race sull'hash, retry e ricalcolo selettivo senza alcun modello.
- [x] M2 Worker: CLI `aicol install|run|status`, ciclo con `LISTEN` più poll, batch per definizione, concorrenza limitata, backend `llm` con output strutturato e backend `embedding` a lotti via OpenRouter, classificazione degli errori (retryable o no), test con provider finto e un test marcato contro OpenRouter vero.
- [ ] M3 Demo: Postgres in Docker, tabella `ticket` con dati realistici in italiano, quattro colonne LLM più un embedding con ricerca dei ticket simili, script che mostra insert, riempimento, errore con retry, cambio prompt con ricalcolo selettivo, override umano che resta. Costo misurato e dichiarato.
- [ ] M4 Demo UI: piccola app che mostra la tabella e le celle che si riempiono, la coda di revisione e il lineage di una cella. Solo se M3 convince.

## Dopo l'esperimento (solo se passa)

Few-shot dagli override umani, chunking con destinazione a tabella (#11), backfill a lotti per tabelle grandi, budget per definizione, packaging come estensione installabile, worker in Rust, estensione SQLite con lo stesso modello concettuale.
