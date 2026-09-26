# Cicli

Un ciclo è un'unità di lavoro chiusa. Il file tiene gli ultimi ~15; i più vecchi vanno in `docs/cycles-archive/`. Le decisioni durevoli stanno in `DECISIONS.md`, le milestone aperte in `ROADMAP.md`.

## Ciclo 1 (26 settembre 2026): dall'idea alla demo end to end

**Obiettivo.** Verificare in una giornata se il modello "colonna derivata mantenuta dal database" regge un ciclo completo su dati realistici, prima di decidere se farne un prodotto.

**Fatto.**

- Analisi di mercato e concorrenza (`ANALYSIS.md`), overview HTML con pro e contro delle decisioni (`overview.html`).
- Estensione SQL (`sql/0001`-`0003`): schema `ai` con catalogo, versioni, coda, lineage, trigger di accodamento e override, contratto worker, viste. `0002` corregge il tipo della vista dei costi, `0003` azzera tentativi e backoff dei job pending quando nasce una versione nuova.
- Worker di riferimento `aicol` (install, run, status): `LISTEN` più poll, batch per backend, backend `llm` con output strutturato, `embedding` a lotti, classificazione degli errori, parse JSON tollerante, drain consapevole del backoff. 27 test di integrazione sull'estensione senza modelli, 11 sul worker con provider finto più uno marcato `llm`.
- Demo ticket di assistenza: 21 ticket, sei colonne derivate, otto passi che coprono coda, ticket nuovo, override che resta, cambio prompt con ricalcolo selettivo, ricerca dei simili via embedding, lineage e costi. Stima di spesa con conferma prima di chiamare i modelli.
- Confronto di otto modelli su `urgency` e `category` contro `gold.json` (`compare_models.py`, tabella in `ANALYSIS.md`): i modelli piccoli del 2026 sono equivalenti a Haiku 4.5 a un decimo del costo; default della demo spostato a `openai/gpt-6-luna` con effort low.
- Backend `decision` (`sql/0004`) per i modelli a risposta tipizzata: TypeSafe Jev via `POST /api/alpha/decisions` di OpenRouter, probabilità calibrate in `ai.result.details`, `complete_job` con parametro `p_details` compatibile. Sui ticket: 100 per cento, 28 volte meno costoso di Haiku 4.5, confidenza che scende davvero sui casi ambigui.

**Decisioni.** #1-#12 in `DECISIONS.md`, tutte prese in questo ciclo; #12 (backend `decision`) estende il perimetro di #10.

**Prossimo passo.** Valutare il criterio di uscita in `ANALYSIS.md` e decidere se fare M4 (demo UI). Aperto: `.env.example` non elenca `OPENROUTER_BASE_URL`, letta dal worker.
