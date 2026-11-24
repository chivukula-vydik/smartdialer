# Load test

Output of `python -m loadtest.load_test` on a 2-core container, SQLite WAL, Python 3.13.18.

## 1. Allocation under contention

Each call = 2 write transactions (reserve agent + claim borrower + insert call; then complete + release). All processes hit the same agent pool.

| processes | agents | calls/s | tx/s | tx p50 ms | tx p99 ms | invariant errors |
|---|---|---|---|---|---|---|
| 1 | 100 | 3870 | 7741 | 0.05 | 4.98 | 0 |
| 4 | 100 | 4749 | 9498 | 0.06 | 4.68 | 0 |
| 8 | 100 | 5326 | 10653 | 0.06 | 6.29 | 0 |
| 8 | 1000 | 3064 | 6129 | 0.13 | 18.65 | 0 |

## 2. One safety decision vs scale

Snapshot (counts by state, outcome windows, abandon window) + binomial bound + permit inserts, in one write transaction. In-flight = 0.3 x agents ringing, 20% of agents free.

| agents | calls in history | decision p50 ms | decision p99 ms | of which snapshot p50 ms | permits/decision |
|---|---|---|---|---|---|
| 100 | 20000 | 3.8 | 5.4 | 3.5 | 9 |
| 1000 | 100000 | 20.6 | 21.2 | 17.5 | 146 |
| 10000 | 200000 | 54.3 | 63.6 | 35.9 | 1847 |

## 3. Event pipeline

20000 events (25% exact duplicates): ingest 31516/s, process 4078/s, invariant errors 0.
