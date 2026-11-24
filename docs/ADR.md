# Architecture decisions

Short form: what I chose, why, what it solves, what it makes harder.

## ADR-1: One relational database is the only shared state

**Chose:** SQLite (WAL) for the prototype; the SQL is written to port directly to Postgres.
No Redis, no Kafka, no coordinator service.

**Why:** The hard part of this problem is a handful of invariants that span several entities at
once: "agent reserved AND borrower claimed AND call row created AND permit consumed", or "call
ANSWERED AND agent bound". Those want a transaction, not a choreography of messages. One store
with row-level compare-and-set gives that for free, and makes crash recovery a query instead of
a replay.

**Solves:** double allocation (CAS + unique indexes), crash consistency (all-or-nothing
transactions), cross-worker pacing (one ledger), auditability (every decision row is kept).

**Makes harder:** the database is the throughput ceiling and a single point of failure. SQLite
specifically has one writer at a time and only works on one machine; it is the prototype
choice because it needs no setup and still gives real cross-process locking. In production
this is Postgres with `SELECT ... FOR UPDATE SKIP LOCKED`, sharded by campaign/team (see
[SCALE.md](SCALE.md)).

**Rejected:** Redis for agent state (fast, but then "reserve agent" and "insert call" are in
two systems and need a saga; and the "DB says X, cache says Y" question appears). Kafka for
provider events (ordering per call helps, but we already have to be correct under reordering
because providers reorder before Kafka sees anything; an inbox table is enough at this scale).

## ADR-2: Compare-and-set plus leases, not distributed locks

**Chose:** every mutation is `UPDATE ... WHERE id=? AND state IN (expected)` and checks rowcount.
Ownership of in-progress work (a call being dialed, a claimed borrower, a reserved agent) is a
lease with an expiry, not a lock held by a live process.

**Why:** a crashed worker cannot release a lock, but its lease expires. Any worker can then
recover the row, and the CAS guarantees only one of them does.

**Makes harder:** every recovery path must first find out what actually happened (ask the
provider), because "lease expired" does not mean "nothing happened". That is
`reconcile_predial`. Lease lengths are tuning parameters (10 s dial lease, 3 s reconcile).

## ADR-3: Safety Controller issues single-use signed permits from a shared ledger

**Chose:** the pacer returns a number; the safety controller recomputes capacity inside a
write transaction, counting other workers' unconsumed permits, and inserts N permit rows (HMAC
signed, 3 s TTL). The allocator consumes one per call with a CAS.

**Why:** it makes the safety boundary structural rather than a convention. The pacer has no
path to the provider, and N workers cannot each be told "15 more" against the same agents.

**Solves:** bypass, multi-worker overshoot, replay of an old decision, auditability ("why did
we dial 17?" is a row in `decisions` with both the pacer's rationale and safety's numbers).

**Makes harder:** a write transaction per decision per worker (cheap: ~4 ms at 100 agents,
~40 ms at 10k, measured). Permits that are not used still occupy budget until they expire, so
TTL must be short.

## ADR-4: Two-layer safety: deterministic ceilings, then a pessimistic statistical bound

**Chose:** hard rules that do not depend on any model (freshness, provider breaker, dial-ratio
ceiling, concurrency ceiling, rolling abandon-rate breaker that forces progressive, cold-start
progressive), then a binomial overflow bound using the upper confidence bound of the answer rate.
Details in [PACING.md](PACING.md).

**Why:** the statistical layer gives utilisation; the deterministic layer bounds the damage if
the statistical layer is wrong.

**Makes harder:** the bound is conservative by construction (assumes all ringing calls could
answer at the same instant against today's free agents), so it leaves utilisation on the table.
Measured cost in [RESULTS.md](RESULTS.md).

## ADR-5: Provider events go through an inbox table and a monotonic state machine

**Chose:** webhooks are `INSERT OR IGNORE` keyed on `(provider, event_id)`; processing is one
transaction that claims the event and applies it. Transitions only move up a rank; terminal
states absorb.

**Why:** exact duplicates are free to drop; semantic duplicates and reordering are handled by the
state machine rather than by trying to restore order (we cannot: providers do not give a
reliable sequence number across retries).

**Makes harder:** the inbox grows forever unless pruned (a retention job, not built). Processing is
serial per worker; at high event rates it needs partitioning by call_id.

## ADR-6: Answer rate measured on resolved dial cohorts, not on recent outcomes

**Chose:** statistics use calls dialed more than 30 s ago (all outcomes known), ordered by dial
time. A separate "answers already seen among calls dialed in the last 30 s" is used as a hard
lower bound so a jump in answer rate is noticed before the cohort resolves.

**Why:** I first used "last N outcomes". It was badly biased: answers resolve in ~8 s,
no-answers in ~25 s, so the newest outcomes are mostly answers. At a true 30% the estimate read
55-60% and the safety controller cancelled ringing calls it did not need to. Found in the
simulator, fixed, documented here because it is the kind of bug that looks like "the model is
conservative".

**Makes harder:** a 30 s lag before a regime change is fully visible (hence the lower bound).

## ADR-7: Python, virtual clock, same code in simulation and tests

**Chose:** Python standard library only (pytest and matplotlib for tests and charts). Every
component takes `now` as a parameter; nothing reads the wall clock. The simulator drives the
real worker, safety, allocator, event and recovery code with mock providers on a virtual clock.

**Why:** 20 simulated minutes run in ~15 s, runs are reproducible from a seed, and the thing
being simulated is the thing being shipped (no separate "model" of the dialer).

**Makes harder:** the simulator is single threaded, so real thread interleavings are only
covered by the concurrency tests and the load test, not by the scenario runs. Provider latency
is modelled as event delay, not as a blocking API call that ties up a worker.

## ADR-8: Per-worker circuit breakers

**Chose:** in-memory breaker per provider per worker.

**Makes harder:** workers disagree for a few calls after an outage starts. A shared breaker in
the DB would converge faster at the cost of a write per dial outcome. Acceptable because safety
also rejects when the breaker of the deciding worker is open, and dials to a dead provider fail
fast.
