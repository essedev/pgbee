# CFPB field test

pgbee on real text: 3000 complaints from the public [CFPB Consumer Complaint Database](https://www.consumerfinance.gov/data-research/consumer-complaints/), 2022-2024, stratified over nine financial products (333 each). One table, four derived columns, the real worker process (`pgbee run`, batch 50, concurrency 8) until the queue is empty. It calls real models on OpenRouter and costs money.

```bash
cd worker && uv run python ../demo/cfpb/field_test.py --limit 60   # cost probe, a fraction of a cent
cd worker && uv run python ../demo/cfpb/field_test.py              # all 3000 rows, about 0.41 USD
```

The script prints the estimate and asks before spending (`--yes` skips the question), recreates the database `aidb_cfpb` on every run and writes `results.json` (`results-N.json` with `--limit N`). [`prepare.py`](prepare.py) rebuilds `sample.jsonl.gz` from the Hugging Face copy of the database.

## Results per column

From [`results.json`](results.json), models and prices of 26 September 2026:

| Column | Backend | Model | Cost per 1000 rows | Mean latency | Agreement with the consumer's label |
|---|---|---|---|---|---|
| `product_jev` | `decision` | `typesafe/jev-1.13` | 0.019 USD | 0.32 s | 79.2% |
| `money_lost` | `decision`, same call | `typesafe/jev-1.13` | 0.019 USD | 0.32 s | |
| `product_llm` | `llm`, low effort | `openai/gpt-6-luna` | 0.093 USD | 1.81 s | 79.7% |
| `embedding` | `embedding`, batches of 100 | `openai/text-embedding-3-small` | 0.005 USD | 0.04 s | |

12,000 jobs, 0 dead, 0 retries, 0.41 USD in total. The two product columns agree with each other on 88.8% of the rows. The label is the product the consumer picked, and consumers sometimes pick wrong: the most frequent confusion (100 cases) is debt collection read as credit reporting, and most of those complaints ask to remove entries from a credit report. So the percentages measure agreement with a noisy label, not accuracy.

Confidence against agreement:

| Confidence | `decision`: rows | `decision`: agreement | `llm`: rows | `llm`: agreement |
|---|---|---|---|---|
| below 0.5 | 113 | 35% | 44 | 32% |
| 0.5-0.7 | 231 | 44% | 87 | 43% |
| 0.7-0.9 | 316 | 59% | 422 | 59% |
| 0.9 and above | 2340 | 88% | 2447 | 86% |

This run took 17.5 minutes with one loop for all backends, so the fast columns advanced at the pace of the LLM. Since then the worker runs one loop per backend; on 600 rows ([`results-600.json`](results-600.json)) the `decision` columns finish in 28 s, the embeddings in 21 s, the LLM in 144 s.
