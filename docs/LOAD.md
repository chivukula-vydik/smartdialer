# Load test

Output of `python -m loadtest.load_test` on a 2-core container, SQLite WAL, Python 3.13.18.

## 1. Allocation under contention

Each call = 2 write transactions (reserve agent + claim borrower + insert call; then complete + release). All processes hit the same agent pool.

| processes | agents | calls/s | tx/s | tx p50 ms | tx p99 ms | invariant errors |
|---|---|---|---|---|---|---|
| 1 | 100 | 2704 | 5408 | 0.07 | 6.56 | 0 |
| 4 | 100 | 3462 | 6923 | 0.08 | 5.72 | 0 |
| 8 | 100 | 3605 | 7210 | 0.08 | 8.6 | 0 |
| 8 | 1000 | 2420 | 4839 | 0.16 | 20.49 | 0 |

## 2. One safety decision vs scale

Snapshot (counts by state, outcome windows, abandon window) + binomial bound + permit inserts, in one write transaction. In-flight = 0.3 x agents ringing, 20% of agents free.

| agents | calls in history | decision p50 ms | decision p99 ms | of which snapshot p50 ms | permits/decision |
|---|---|---|---|---|---|
| 100 | 20000 | 0.8 | 1.2 | 0.5 | 9 |
| 1000 | 100000 | 3.9 | 4.6 | 0.7 | 146 |
| 10000 | 200000 | 24.2 | 31.5 | 1.4 | 1847 |

## 3. Event pipeline

20000 events (25% exact duplicates): ingest 24067/s, process 3287/s, invariant errors 0.
