# Benchmarks

Scripts that anyone can rerun on a laptop, with the demo Postgres (`make db-up`) and nothing paid: no script here calls a real model. Results of the runs quoted in the documentation are in [`results/`](results/).

| Script | Question | Make target |
|---|---|---|
| [`failure/chaos.py`](failure/chaos.py) | Does keeping the state in the database matter when processes crash, the prompt changes and people edit rows? | `make bench-failure seed=7` |
| [`scale.py`](scale.py) | What do declaring a column, bulk loads and claims cost on a million rows? | `make bench-scale` |
| [`lanes.py`](lanes.py) | How much does one lane per backend save over one loop for all? | `make bench-lanes` |

## Failure injection

The same workload runs against three systems, each with its own database, application process and two worker processes:

- **pgbee**: the application only writes rows; the triggers enqueue, the real `pgbee run` worker computes.
- **naive**: the usual app-side design. The application saves the row, commits, then puts the row id on an external queue; a worker reads the row, calls the model, writes the value.
- **outbox**: the careful app-side design. The application writes the row and the queue entry in one transaction (transactional outbox); same worker.

The baselines get a good queue, not a strawman: durable, at-least-once, with leases, so a worker that dies loses nothing; failed calls are retried; the enqueue after commit is retried three times; after the prompt change a reprocess script puts every row back on the queue and is rerun until it completes.

The workload (`failure/scenario.py`, 2000 rows by default) mixes inserts, edits through the application soon after the insert (the window in which a row changes while its value is computed), edits by a script that bypasses the application (a migration, a psql session, another service), one prompt change halfway, and human corrections of about 3% of the rows. The model is a local fake server that answers a hash of prompt and text after 20-300 ms, so the right value of every row is known, and fails 3% of calls with a 5xx. Faults, identical for the three systems: a random process (application or worker) killed with SIGKILL about every 60 operations and restarted a second later, and two 4-second outages of the naive design's external queue. When the load ends and the queues are empty, every row is compared with its right value.

Seeds 7 / 11 / 23, 2000 rows and 46-47 kills each ([`results/failure-seed*.json`](results/)):

| | pgbee | naive | outbox |
|---|---|---|---|
| Rows left without a value | 0 / 0 / 0 | 4 / 1 / 3 | 0 / 0 / 0 |
| Stale values that look fresh | 0 / 0 / 0 | 30 / 49 / 41 | 15 / 35 / 44 |
| of which computed on an older text | 0 / 0 / 0 | 22 / 42 / 27 | 11 / 29 / 29 |
| of which computed with the old prompt | 0 / 0 / 0 | 8 / 7 / 14 | 4 / 6 / 15 |
| Human corrections overwritten (of 56 / 57 / 60) | 0 / 0 / 0 | 26 / 27 / 20 | 30 / 30 / 21 |
| Model calls | 3436 / 3563 / 3604 | 3774 / 3724 / 3736 | 3787 / 3751 / 3733 |

pgbee also knows its own state at the end: `bee.stale_rows` and `bee.dead_jobs` are empty and every correction is listed as a human override. The app-side designs have no way to tell which of their values are wrong.

The runs shared the machine (an Apple Silicon laptop, Postgres in Docker) with an unrelated workload that used most of the Docker CPUs, so the load phase lasted 3 to 4.5 minutes instead of about 1.5. Stale values in the app-side designs grow with how long rows wait in the queue: on a quiet machine expect fewer of them, not zero. The zeros of pgbee do not depend on timing.

Where the errors of the app-side designs come from:

- **Human corrections overwritten**: the reprocess after the prompt change, or a job still in the queue, writes over a value a person set. Nothing in those designs knows that a value came from a person. pgbee records it as an override and never recomputes it (`override_policy` can release it when the source changes).
- **Stale values that look fresh**: a row edited while its value was being computed gets the value of the old text, when the older job finishes last; a row edited by a script is never enqueued at all; a job that read the old prompt writes after the reprocess. pgbee compares the hash of the sources and the version at completion time and queues the row again.
- **Rows without a value** (naive only): the row was saved but the enqueue failed, during the queue outage or when the application died between commit and enqueue. The outbox closes this hole, and that is all it closes.

What this does not show: the fake model has no quality to measure, the load is synthetic and runs on one machine, and an app-side design can add versions, source hashes and override flags. That is the point of the test: those are the parts pgbee is made of. The app-side designs also made 4-10% more model calls: every edit adds a job even when the row's previous job has not run yet (pgbee keeps one live job per row and column and updates it), and the reprocess recomputes the corrected rows too.

The test also found a bug in pgbee. An application UPDATE locks the row and then, through the trigger, the job; `complete_job` locked the job first and the row after, so a row edited while its value was being completed deadlocked, and Postgres killed one of the two transactions, sometimes the application's. Fixed in `sql/0014` (the row is locked first), with a test that reproduces it; the worker also no longer exits when the database refuses a result. The table above is after the fix.

Setting a cell to the value it already holds is not an override in pgbee: ORMs often rewrite every column on save. The scenario's corrections always change the value.

## Scale

`scale.py` runs three cases on a table of one million rows, each on a fresh database: `backfill` (declare a column on a full table while another session inserts), `bulk-trigger` (load a million rows with the column active), `bulk-disabled` (the recommended recipe: `bee.disable`, load, `bee.enable`). Measured in cycles 3 and 4 ([`docs/CYCLES.md`](../docs/CYCLES.md)) on an Apple Silicon laptop with Postgres in Docker, with the same queries:

| Case | Result |
|---|---|
| `add_column` on 1M rows | 0.06 s (39 s before the chunked backfill, decision #15) |
| An insert from another session during the declaration | waits 0.01 s (38.9 s before) |
| First claim, which also enqueues the next chunk | 27 ms |
| Load of 1M rows with the column active | 38 s, against 3.7 s with no derived column: one job per row, written by the trigger |
| Same load with the column disabled, then `bee.enable` | 6.7 s, then 19 ms for `enable` (the backfill takes over in chunks) |
| Claim on a queue of 1M fresh jobs | 1-3 s until autovacuum analyzes `bee.job`, milliseconds after |

The rerun of 27 September shared the machine with a workload that made everything about 13 times slower (the disabled load took 90 s), so its absolute times are not reported; the ratios held (the load with the trigger took 6 times the disabled one, 5.7 before).

## Lanes

`lanes.py` gives 200 rows three derived columns, one per backend, and processes them twice with a provider that only waits for the median latencies of the field test (llm 1.87 s, decision 0.31 s, embedding 0.03 s per call): first with one worker loop claiming every backend, then with one lane per backend as `pgbee run` does. Seconds until each column is full ([`results/lanes.json`](results/lanes.json)):

| Column | One loop | One lane per backend |
|---|---|---|
| `embedding` | 116.3 | 4.3 |
| `decision` | 116.7 | 18.4 |
| `llm` | 120.1 | 98.1 |

With one loop a batch mixes backends and ends when its slowest call ends, so every column advances at the pace of the LLM. With lanes the LLM takes what its concurrency allows (200 calls, 4 at a time, 1.87 s each: 93.5 s at best) and the fast columns are not held back. The field test saw the same on real models (decision #20).
