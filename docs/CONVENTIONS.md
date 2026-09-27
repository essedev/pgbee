# Convenzioni di codice

Regole specifiche di questo progetto. Gli standard trasversali sono nel CLAUDE.md globale.

## Generali

- Line length 100. Identificatori, codice, SQL e commenti in inglese. Prosa della demo (ticket, prompt di esempio) in italiano.
- Doc per chi usa il progetto in inglese: `README.md`, `SECURITY.md`, `docs/ARCHITECTURE.md`, `examples/`. Note di lavoro in italiano: `ANALYSIS`, `DECISIONS`, `DATABASE_SCHEMA`, `CYCLES`, `ROADMAP`, `CONVENTIONS`, `CLAUDE.md`. Una modifica all'architettura aggiorna `ARCHITECTURE.md` in inglese.
- Nomi dei test in inglese.

## SQL (`sql/`)

- Un file per versione, `NNNN_descrizione.sql`, applicato una volta e mai riscritto dopo il primo commit che lo contiene. Correzioni in un file nuovo. Il file `NNNN` è anche la versione `0.N` dell'estensione (`pgbee extension-files`).
- Tutto nello schema `bee`. Tipi enum creati con `DO $$ ... IF NOT EXISTS` così lo script regge una seconda esecuzione parziale.
- SQL dinamico solo con `format()` e `%I` per gli identificatori, `%L` mai per valori che arrivano dall'esterno: si usano `EXECUTE ... USING`.
- Le funzioni del contratto worker sono stabili: cambiarne la firma è una decisione in `DECISIONS.md` e una versione nuova della funzione, non una modifica in place.
- Ogni funzione pubblica ha `COMMENT ON FUNCTION` con una riga di descrizione: è la documentazione che `\df+` mostra.
- Trigger per tabella utente con prefisso `bee_` e nome della colonna (`bee_enqueue_<colonna>`, `bee_override_<colonna>`), così `\d tabella` dice chi li ha messi.

## Python (`worker/`)

- Type hints ovunque, `mypy --strict`. Async per tutto l'I/O.
- Errori dei provider classificati in una funzione sola (`retryable` o no): 429, 5xx, timeout sono retryable; 4xx di validazione e schema non rispettato dopo il tentativo di riparazione no.
- Niente `except: pass`. Un errore che non si sa gestire finisce in `fail_job` con il messaggio intero.
- Logging strutturato (`structlog`), un evento per job con id, definizione, esito, latenza, token.
- Il worker non contiene SQL su tabelle utente: solo chiamate alle funzioni e alle viste dello schema `bee`.
- Configurazione da variabili d'ambiente con `pydantic-settings`; nessun file di config.

## Test

- Integration su Postgres vero in Docker (compose del progetto), un run alla volta. Il conftest applica gli script di `sql/` da zero su un database dedicato.
- I test dell'estensione non usano modelli: chiamano `complete_job` e `fail_job` a mano per simulare il worker.
- I test del worker usano un provider finto etichettato come tale; i test contro OpenRouter vero portano il marker `llm` e sono esclusi dal giro di default.
- I test che servono l'immagine con l'estensione (`CREATE EXTENSION`, catena di update, dump e restore) portano il marker `extension` e girano solo con `make test-extension`, su un container usa e getta.

## Changelog e rilasci

- `CHANGELOG.md` alla radice, in inglese, formato Keep a Changelog: sezione `## [Unreleased]` in cima, poi una sezione per rilascio, sottosezioni `Added`, `Changed`, `Deprecated`, `Removed`, `Fixed`, `Security`, `Upgrading`. `.github/scripts/changelog.py check` lo valida in CI.
- Ogni modifica che chi usa pgbee può notare aggiorna `Unreleased` nello stesso commit: funzioni o viste SQL, contratto del worker, CLI, variabili d'ambiente, comportamento, immagini. Refactor, test e doc interne no.
- Si scrive per chi usa il progetto: cosa cambia per lui e cosa deve fare, non come è stato implementato. Una voce per cambiamento, con i nomi esatti (`bee.complete_job`, `PGBEE_PROVIDER`).
- `Upgrading` è obbligatoria quando arrivano file SQL (quale versione dell'estensione, `pgbee install` o `ALTER EXTENSION pgbee UPDATE`), quando cambia il contratto del worker o quando serve un'azione manuale.
- Rilascio, ogni passo su comando di Simone: versione in `worker/pyproject.toml` (più `uv lock`), `make changelog-release version=X.Y.Z` (sposta `Unreleased` nella versione con la data di oggi e aggiorna i link), commit `chore(release): X.Y.Z`, push, tag `vX.Y.Z` e push del tag. `release.yml` usa la sezione della versione come testo della release GitHub e si ferma prima di pubblicare se manca.

## Pulizia

- Niente codice morto, niente TODO senza voce in ROADMAP.
- La demo usa solo l'interfaccia pubblica: se ha bisogno di una scorciatoia, manca qualcosa al prodotto.
