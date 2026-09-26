---
paths:
  - "sql/*.sql"
---

# Estensione SQL

- **Un file applicato non si riscrive**: `sql/NNNN_*.sql` già committato resta com'è, la correzione va in un file nuovo con numero successivo. `ai.schema_version` traccia cosa è stato applicato.
- **Idempotenza dove costa poco**: enum e tipi con `IF NOT EXISTS` o `DO $$ ... EXCEPTION WHEN duplicate_object`, così un install interrotto si può rilanciare.
- **SQL dinamico solo con `format()` e `%I`** per gli identificatori; i valori passano da `EXECUTE ... USING`, mai interpolati con `%L` quando arrivano dall'esterno.
- **`CREATE OR REPLACE` azzera `SECURITY DEFINER` e `SET search_path`** (i grant invece restano): ridefinendo una funzione del contratto o un trigger, ripeti i due attributi nella definizione. `test_roles.py` lo verifica.
- **Dentro una funzione definer niente nomi risolti dal `search_path`**: tabelle e tipi qualificati con lo schema (`ai.job`, il `vector` di pgvector con il suo schema).
- **Le funzioni del contratto worker sono stabili**: firma nuova significa funzione nuova e voce in `docs/DECISIONS.md`.
- **`COMMENT ON FUNCTION` su ogni funzione pubblica**, una riga: è la doc che `\df+` mostra.
- **`docs/DATABASE_SCHEMA.md` cambia nello stesso commit** del file SQL che tocca tabelle, viste o funzioni pubbliche.
