# Analisi: idea, mercato, concorrenza

Stato al 26 settembre 2026. Fonti in coda al file.

## L'idea in una frase

Colonne derivate affidabili per PostgreSQL: dichiari che una colonna si ricava da altre colonne tramite un modello (classificazione, estrazione strutturata, riassunto, embedding), e il database si occupa di coda, batch, retry, versionamento di prompt e modello, lineage per riga, confidenza e override umano. Il modello è uno dei backend possibili, non il prodotto.

## Cosa non è

Non è "una funzione SQL che chiama l'LLM". Quella esiste da anni (pgai, pg_ai, postgres-llm, Cortex, Databricks AI functions) e si copia in un fine settimana. Il valore sta nella parte che nessuno ha fatto bene: la garanzia che ogni riga prima o poi abbia il suo valore, che si sappia quale modello e quale prompt l'hanno prodotto, che cambiare il prompt ricalcoli solo ciò che serve, e che una correzione umana non venga sovrascritta.

## Perché ora

- Postgres assorbe lo stack: vettori, code, cron, search. Chi ha un job batch Python che etichetta righe lo vuole eliminare, non mantenere.
- Airtable (Field Agents) e Notion (AI Autofill) hanno reso "la colonna che si compila da sola" una feature attesa. Nessuno la offre a chi ha un Postgres.
- Il concorrente più vicino ha lasciato lo spazio: pgai di Timescale è archiviato (non mantenuto da febbraio 2026, repo in sola lettura da maggio) e Tiger Cloud ha rimosso vectorizer e funzioni `ai.*` il 30 giugno 2026, consigliando di spostare le chiamate ai provider nel codice applicativo.

## Il dubbio da sciogliere

L'uscita di Timescale ammette due letture. La prima: non c'è domanda pagante, chi lavora seriamente preferisce il job in codice. La seconda: l'architettura sincrona "il DB chiama il provider" era il problema (governance, rate limit, latenza dentro la transazione) e Timescale ha preferito tornare al core invece di riprogettare. Il vectorizer, che aveva già coda e worker esterno, è morto insieme alle funzioni sincrone: questo pesa a favore della prima lettura. Il progetto nasce come esperimento a tempo per rispondere a questa domanda, non come prodotto deciso.

## Panorama verificato

| Chi | Cosa fa | Cosa manca rispetto all'idea |
|---|---|---|
| pgai (Timescale) | Funzioni SQL sincrone verso i provider più vectorizer con coda e worker Python | Archiviato. Solo embedding nel vectorizer, niente versioning per riga, confidenza, override |
| Supabase automatic embeddings | Guida da assemblare: trigger, pgmq, pg_cron, Edge Function. Batch e retry | Solo embedding. Niente versioning, confidenza, override, ricalcolo al cambio config. Non è un prodotto |
| pg_vectorize (Tembo) | Attivo. Embedding job su pgmq con worker | Solo embedding |
| postgres-llm (JigsawStack) | Trigger, coda, pg_cron, output JSON strutturato, retry. Il più vicino come concetto | 56 stelle, 14 commit. Niente batch, versioning, confidenza, override. Il DB chiama fuori via estensione `http` |
| PostgresML | Modelli dentro Postgres con GPU | Altro problema (inferenza in-DB), pesante da operare |
| SQLite-AI (SQLite Cloud) | Modelli GGUF locali, embedding, chat, Whisper da SQL | Funzioni da chiamare, non colonne mantenute |
| Snowflake Cortex, Databricks, BigQuery | `AI_CLASSIFY`, `ai_extract` nativi | Warehouse. Chiamate sincrone nella query, non colonne mantenute con lineage |
| Airtable Field Agents, Notion Autofill | La stessa idea lato no-code | Chiusi, SaaS, nessuna versione per sviluppatori |

Nessuno oggi fa colonne derivate generiche con lineage, versioning e override su Postgres.

## Perimetro

Tre backend e basta: `llm` (classificazione, estrazione strutturata, riassunto), `embedding` (vettori per la ricerca semantica) e `custom` (un worker dell'utente calcola il valore). Sono lo stesso meccanismo: un valore derivato da colonne sorgente, prodotto fuori dal database, da tenere aggiornato. Cambiare il modello di embedding e rifare tutti i vettori è la stessa operazione di cambiare un prompt. Fuori perimetro, per scelta (`DECISIONS.md` #10): job senza riga di destinazione, cron, workflow, join semantici, ricerca e RAG (li fanno pgvector e Postgres, noi li alimentiamo), memoria conversazionale, agenti.

## Perché Postgres e non SQLite (per ora)

Il fossato è asincrono: coda, batch, retry, concorrenza tra worker. SQLite è una libreria senza processo proprio, quindi il lavoro in background lo deve guidare l'app host. Un'estensione SQLite validerebbe il modello dati ma non la parte difficile. SQLite resta un secondo prodotto con lo stesso modello concettuale (app locali, agenti, tool desktop), da affrontare quando il modello è stabile.

## Perché non "il DB chiama fuori"

- Le aziende con dati sensibili vietano che il processo database apra connessioni verso terzi.
- Senza un punto centrale non si loggano i dati inviati, non si maschera PII, non si impongono quote.
- I Postgres gestiti (RDS, Supabase, Neon) non caricano estensioni compilate arbitrarie: un'estensione in SQL puro si installa ovunque.

Quindi: l'estensione possiede catalogo, coda, lineage e regole; un worker esterno, sotto il controllo di chi lo installa, preleva i job e parla con i modelli. Il contratto tra i due è SQL pubblico, così un'azienda può scrivere il proprio worker dietro il proprio gateway.

## Criterio di uscita

La demo mostra il ciclo completo su dati realistici: inserimento, coda, batch, retry sotto errore, cambio prompt con ricalcolo selettivo, override umano che resta. Se il ciclo regge senza interventi manuali e l'esperienza "aggiungi la colonna e non tocchi più niente" convince, si passa alla fase prodotto (packaging, worker compilato, chunking, SQLite). Altrimenti si chiude e resta il modello concettuale.

## Fonti

- pgai, repository archiviato: https://github.com/timescale/pgai
- Tiger Data, deprecazione vectorizer e chiamate LLM in-database: https://www.tigerdata.com/docs/deploy/tiger-cloud/vectorizer-deprecation
- Supabase, automatic embeddings: https://supabase.com/docs/guides/ai/automatic-embeddings
- pg_vectorize: https://github.com/tembo-io/pg_vectorize
- postgres-llm: https://github.com/JigsawStack/postgres-llm
- PostgresML: https://github.com/postgresml/postgresml
- SQLite-AI: https://www.sqlite.ai/sqlite-ai
- Snowflake Cortex AI functions: https://docs.snowflake.com/en/user-guide/snowflake-cortex/aisql
- Airtable AI fields: https://support.airtable.com/docs/using-airtable-ai-in-fields
- Notion AI autofill: https://www.notion.com/help/autofill
