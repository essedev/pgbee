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

Quattro backend e basta: `llm` (classificazione, estrazione strutturata, riassunto), `decision` (modelli a risposta tipizzata con probabilità calibrate, per classificazione e flag), `embedding` (vettori per la ricerca semantica) e `custom` (un worker dell'utente calcola il valore). Sono lo stesso meccanismo: un valore derivato da colonne sorgente, prodotto fuori dal database, da tenere aggiornato. Cambiare il modello di embedding e rifare tutti i vettori è la stessa operazione di cambiare un prompt. Fuori perimetro, per scelta (`DECISIONS.md` #10): job senza riga di destinazione, cron, workflow, join semantici, ricerca e RAG (li fanno pgvector e Postgres, noi li alimentiamo), memoria conversazionale, agenti.

## Perché Postgres e non SQLite (per ora)

Il fossato è asincrono: coda, batch, retry, concorrenza tra worker. SQLite è una libreria senza processo proprio, quindi il lavoro in background lo deve guidare l'app host. Un'estensione SQLite validerebbe il modello dati ma non la parte difficile. SQLite resta un secondo prodotto con lo stesso modello concettuale (app locali, agenti, tool desktop), da affrontare quando il modello è stabile.

## Perché non "il DB chiama fuori"

- Le aziende con dati sensibili vietano che il processo database apra connessioni verso terzi.
- Senza un punto centrale non si loggano i dati inviati, non si maschera PII, non si impongono quote.
- I Postgres gestiti (RDS, Supabase, Neon) non caricano estensioni compilate arbitrarie: un'estensione in SQL puro si installa ovunque.

Quindi: l'estensione possiede catalogo, coda, lineage e regole; un worker esterno, sotto il controllo di chi lo installa, preleva i job e parla con i modelli. Il contratto tra i due è SQL pubblico, così un'azienda può scrivere il proprio worker dietro il proprio gateway.

## Confronto modelli per la classificazione (26 settembre 2026)

Otto modelli: sette LLM scelti incrociando l'indice di intelligenza e il tempo al primo token di Artificial Analysis (snapshot pubblico giornaliero, la chiave API fornita risultava non valida) con prezzi e supporto dell'output strutturato su OpenRouter, più Jev (TypeSafe) aggiunto dopo come modello di decisione. Misurati con `demo/compare_models.py` sulle colonne `urgency` e `category` dei 21 ticket della demo, contro le etichette di `demo/gold.json`. Campione piccolo: una riga vale il 5 per cento, le differenze sotto il 10 per cento non sono significative. Costo per 42 chiamate.

| Modello | Reasoning | Accuratezza | USD | Latenza media |
|---|---|---|---|---|
| typesafe/jev-1.13 (backend `decision`) | n/a | 100% | 0,0008 | 0,4 s |
| xiaomi/mimo-v2.6-pro | off | 100% | 0,0023 | 2,3 s |
| openai/gpt-6-luna | low | 100% | 0,0018 | 1,5 s |
| anthropic/claude-haiku-4.5 | default | 100% | 0,0213 | 1,2 s |
| deepseek/deepseek-v4.1-flash | off | 98% | 0,0023 | 1,1 s |
| google/gemini-3.7-flash | low | 98% | 0,0267 | 2,5 s |
| z-ai/glm-5.3-flash | low | 98% | 0,0016 | 2,1 s |
| mistralai/ministral-14b-2512 | n/d | 93% | 0,0007 | 0,7 s |

Letture: sul compito "classifica una riga corta" i modelli piccoli del 2026 sono equivalenti a Haiku 4.5 a un decimo del costo; Gemini Flash a effort low produce comunque migliaia di token di reasoning e costa come Haiku; GLM 5.3 Flash non permette di disattivare il reasoning (errore 400, il job finisce `dead`, comportamento corretto); Ministral 14B sbaglia dove serve giudizio (URGENTE scritto dal cliente su una richiesta amministrativa). Il default della demo passa a `openai/gpt-6-luna` con effort low. La confidenza auto-riportata è risultata quasi sempre 0,85 o più, anche sulle risposte sbagliate: conferma la decisione #6 di chiamarla euristica.

## Modelli di decisione: TypeSafe Jev

Jev (TypeSafe AI, accesso anticipato dal 15 settembre 2026) non è un LLM: in un passaggio parallelo risponde a domande tipizzate, scelta fra classi, vero/falso, punteggio su rubrica, con probabilità calibrate, in 70-500 ms, a 0,042 $ per milione di token in ingresso e output gratuito. È esattamente la forma "classificatore con confidenza vera" che il progetto voleva fin dall'inizio, e ha portato al backend `decision` (`DECISIONS.md` #12).

Accesso: le registrazioni dirette su TypeSafe sono sospese dal 22 settembre 2026 per la domanda; OpenRouter lo serve sull'endpoint `POST /api/alpha/decisions` con il modello `typesafe/jev-1.13`, quindi basta la chiave OpenRouter. Attenzione a `typesafe/jev-router` su OpenRouter: è un router generico verso LLM (la prova è finita su gpt-6-luna), non Jev.

Sui 21 ticket: 100 per cento su urgency e category, 0,0008 $ per 42 domande (28 volte meno di Haiku 4.5), 0,4 s a chiamata. La confidenza media è 0,93 e la minima 0,40: le cinque risposte sotto 0,8 sono i ticket 7, 9, 13, 14 e 19, cioè quattro dei cinque casi che anche le etichette di riferimento considerano ambigui. A differenza degli LLM, quando il ticket è ambiguo il numero scende davvero, ed è questo che rende utile la coda di revisione. Limiti dichiarati dal produttore: niente generazione di testo, 255 classi al massimo, inglese come lingua primaria (l'italiano ha funzionato sul campione), aritmetica e date deboli, sensibile a istruzioni iniettate nel testo. Una chiamata può contenere molte domande sullo stesso stato quasi allo stesso costo: più colonne `decision` sulla stessa riga vanno in una chiamata sola (`DECISIONS.md` #14).

## Criterio di uscita

La demo mostra il ciclo completo su dati realistici: inserimento, coda, batch, retry sotto errore, cambio prompt con ricalcolo selettivo, override umano che resta. Se il ciclo regge senza interventi manuali e l'esperienza "aggiungi la colonna e non tocchi più niente" convince, si passa alla fase prodotto (worker compilato, chunking, SQLite; il packaging è arrivato prima, `DECISIONS.md` #18). Altrimenti si chiude e resta il modello concettuale.

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
