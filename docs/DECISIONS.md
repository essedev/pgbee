# Decisioni

Voci numerate, citabili come `#N`. Entra solo ciò che vincola il futuro con un'alternativa scartata. Status: proposta, attiva, superata.

## #1 Estensione in SQL puro, non compilata

Status: attiva. L'estensione è SQL più PL/pgSQL, installata da uno script versionato e non da `CREATE EXTENSION`. Alternativa scartata: Rust con pgrx. Motivo: i Postgres gestiti non caricano estensioni compilate arbitrarie, e tutto ciò che serve alla v1 (tabelle, trigger, funzioni, `SKIP LOCKED`, `NOTIFY`) è disponibile in SQL. Il packaging come estensione vera e l'accesso al planner sono lavoro della fase prodotto, se ci si arriva.

## #2 Il database non chiama i provider

Status: attiva. Le chiamate ai modelli le fa un worker esterno che consuma la coda. Alternativa scartata: `pg_net`, estensione `http` o background worker in-process, come pgai e postgres-llm. Motivo: governance (il DB come client di rete verso terzi è vietato quasi ovunque), rate limit e latenza fuori dalla transazione, contratto pubblico che permette worker propri dietro un gateway.

## #3 Worker di riferimento in Python, Rust rinviato

Status: attiva. Il worker è un pacchetto Python con `uv`, psycopg 3 async e SDK OpenAI puntato a OpenRouter. Alternativa scartata: Rust o Go da subito, che darebbero il binario singolo che il mercato si aspetta. Motivo: il progetto è un esperimento a tempo e il contratto SQL è identico; il worker si riscrive senza toccare l'estensione se l'esperimento passa.

## #4 Stale calcolato dalle versioni, non memorizzato

Status: attiva. Ogni risultato riferisce la `column_version` che l'ha prodotto; una riga è stale quando quella versione non è la corrente. Alternativa scartata: colonna di stato per riga aggiornata al cambio prompt. Motivo: nessuno stato da tenere sincronizzato, il ricalcolo incrementale è una query, e il lineage per riga viene gratis.

## #5 Override umano pinnato di default

Status: attiva. Un valore scritto da un umano non viene più ricalcolato, né al cambio prompt né al cambio sorgenti, finché non si toglie il pin. Alternativa scartata: ricalcolare al cambio delle sorgenti (disponibile come policy `until_source_change`). Motivo: chi corregge a mano vuole che la correzione resti; perdere una correzione umana costa più di un valore vecchio.

## #6 Confidenza auto-riportata, dichiarata euristica

Status: attiva. La confidenza è un numero che il modello include nell'output strutturato. Alternativa scartata: logprobs, non disponibili in modo uniforme via OpenRouter; oppure niente confidenza. Motivo: serve un segnale per la coda di revisione e per la policy `hold`; un'euristica dichiarata è meglio dell'assenza, purché la documentazione non la spacci per probabilità.

## #7 psycopg senza ORM nel worker

Status: attiva. Il worker usa psycopg 3 async e SQL esplicito verso le funzioni dello schema `ai`. Alternativa scartata: SQLAlchemy 2.0 async con Alembic, lo stack di default. Motivo: lo schema lo possiede l'estensione, non il Python; un ORM duplicherebbe il modello e Alembic sarebbe una seconda sorgente di migrazioni.

## #8 Prompt senza template

Status: attiva. Il prompt è l'istruzione; il worker aggiunge le sorgenti in un blocco fisso `nome_colonna: valore`. Alternativa scartata: placeholder `{{colonna}}` nel prompt. Motivo: versioni più semplici, hash delle sorgenti indipendente dal prompt, nessun motore di template da mantenere. Si riapre se un caso reale lo richiede.

## #9 Documentazione interna in italiano, README in inglese

Status: attiva. `docs/` e `CLAUDE.md` in italiano, `README.md`, codice, SQL e commenti in inglese. Alternativa scartata: tutto in inglese da subito. Motivo: oggi l'unico lettore della doc interna è italiano; il README è la faccia pubblica di un tool pensato per essere open source. Si traduce quando il progetto apre.
