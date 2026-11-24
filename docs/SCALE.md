# Scale: 100 -> 1,000 -> 10,000 agents

Measured numbers are from [LOAD.md](LOAD.md) (2-core container, SQLite WAL).

## What the load is

Per agent, at a 20% answer rate and ~130 s per conversation cycle: ~0.008 connects/s, ~0.04
dials/s, ~0.15 provider events/s, ~0.25 write transactions/s.

| Agents | Dials/s | Events/s | Write tx/s | Safety decisions/s (1 per worker per s) |
|---|---|---|---|---|
| 100 | 4 | 15 | 25 | N workers |
| 1,000 | 40 | 150 | 250 | N workers |
| 10,000 | 400 | 1,500 | 2,500 | N workers |
| 100,000 | 4,000 | 15,000 | 25,000 | N workers |

The prototype sustains 3,000-5,000 contended allocations/s (two transactions each) and ~4,000
events/s on one SQLite file, so raw allocation throughput is not what breaks first.

## What breaks first: the safety decision holds the global write lock while it scans

One decision = a full snapshot (agent counts by state, active calls grouped by state, the last
400 resolved outcomes, abandons and connects in a 15-minute window) + the bound + permit inserts,
all inside one write transaction so that N workers cannot overshoot.

| Agents | Decision p50 | of which snapshot |
|---|---|---|
| 100 | 4.6 ms | 4.4 ms |
| 1,000 | 19.4 ms | 16.7 ms |
| 10,000 | 55.6 ms | 36.3 ms |

The snapshot is O(rows) and grows with agents and with the abandon window. With 10 workers each
deciding once per second at 10k agents, the write lock is held for decisions ~55% of every
second, and every allocation and event transaction queues behind it. It is the serialization
point by design (that is what makes the budget global), so it must be made cheap, not removed.

**Fix, in order:**

1. **O(1) snapshot.** Keep a `pool_counters` row (agents by state, unbacked/backed in flight,
   open permits) updated inside the same transactions that change state, and per-minute
   bucket rows for outcomes/connects/abandons. The decision reads a handful of rows instead of
   scanning tables. Same correctness (counters change in the same transaction as the state
   they count); decision cost becomes independent of agent count.
2. **One decider per pool, not per worker.** A lease-held pacing leader per pool makes the
   decision once per second and publishes permits; other workers only consume permits and
   process events. Decisions/s becomes "number of pools", not "number of workers".
3. **Shard the ledger by agent pool.** An agent only takes calls for its pool (team / skill /
   campaign), so pools are independent safety domains with their own counters row, permits and
   breaker state. 10k agents = e.g. 50 pools of 200: 50 hot rows instead of one. Trade-off:
   predictive efficiency comes from pooling, and very small pools (< ~50 agents) get noticeably
   less overdial room from the same bound. Pools of a few hundred keep almost all of it.

## What breaks second

* **SQLite's single writer.** Move to Postgres: row locks instead of a database lock,
  `FOR UPDATE SKIP LOCKED` for agent and borrower claims so workers take different rows instead
  of queueing on the same one, partial unique indexes carry over unchanged. Then shard databases
  by pool group when one primary's write rate is the limit (around the 100k-agent row above).
* **Event inbox in the same database as allocation.** At 1,500 events/s it competes with
  allocation for the write path. Move ingestion to an append-only log partitioned by call_id
  (Postgres partitioned table first, Kafka only if that is not enough), consumers per partition.
  Per-call partitioning gives per-call ordering for free, though the state machine stays
  order-independent because providers reorder before we see anything.
* **Agent presence writes.** 10k agents heartbeating every 5 s is 2,000 writes/s of pure noise.
  Batch them, or hold presence in an in-memory TTL store that feeds logout transitions into the
  DB via CAS. That store is an input, never a source of truth for reservations: this is where
  "the cache says RESERVED, the DB says AVAILABLE" would come from, and the DB wins.
* **Borrower selection.** The load test found claim was sorting every pending borrower (index
  did not match the ORDER BY): 2.4x slower at 1k agents. Fixed with a matching index. At tens of
  millions of borrowers with changing priorities, precompute a per-pool dial queue.

## What does not go away with more servers

* **Provider CPS and concurrency limits.** 10k agents at a 20% answer rate need ~400 dials/s;
  trunks and accounts are rate limited. That needs multi-trunk / multi-provider routing with the
  concurrency ceiling enforced per trunk in the safety controller (`max_concurrent_calls` is the
  hook).
* **Webhook latency.** It eats the 2 s abandon grace regardless of our capacity (provider B:
  78 late connects in 20 minutes).
* **Statistics at small scale.** The predictive bound depends on pool size; splitting a campaign
  into many tiny pools to scale the database makes the dialer less efficient.

## 1,000 -> 100,000 agents: order of failure

1. Decision snapshot under the global lock (fix: counters + one leader per pool).
2. Single-writer database (fix: Postgres, then shard by pool group).
3. Event ingestion sharing the write path (fix: partitioned log).
4. Provider CPS / trunk concurrency (fix: routing and per-trunk ceilings; a commercial problem
   as much as a technical one).
5. Presence and housekeeping scans (fix: batching, TTL store as input, indexed sweeps).
