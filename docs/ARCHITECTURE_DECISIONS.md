# Architecture decisions

The main design choices in SmartDialer, with the reasoning and cost of each. How the system works is described in the [README](../README.md).

## 1. One relational database as the only shared state

* **What:** SQLite (WAL mode) for the prototype, with SQL written to port directly to Postgres.
  No Redis, Kafka, message queue or coordinator service.
* **Why:** the core invariants span several rows at once: agent reserved AND borrower claimed AND
  call created AND permit consumed, or call ANSWERED AND agent bound. A transaction expresses that
  directly; a set of messages between services would need a saga to keep them consistent.
* **Solves:** double allocation (compare-and-set + unique indexes), crash consistency
  (all-or-nothing transactions), a single pacing budget across workers, and a full audit trail
  of decisions. Crash recovery becomes a query instead of a replay.
* **Makes harder:** the database is the throughput ceiling and a single point of failure. SQLite
  allows one writer at a time and runs on one machine; past ~1,000 agents this becomes Postgres
  with `FOR UPDATE SKIP LOCKED`, sharded by agent pool (see [Performance and scaling](../README.md#performance-and-scaling)).
  Rejected alternatives: Redis for agent state (reservation and call creation would span two
  stores that can disagree); Kafka for events (ordering must be handled anyway, because providers
  reorder before ingestion; an inbox table is enough at this scale).

## 2. Compare-and-set plus leases instead of distributed locks

* **What:** every mutation is `UPDATE ... WHERE id=? AND state IN (expected)` with a rowcount
  check. Work in progress (a call being dialed, a claimed borrower, a reserved agent) is owned
  through a lease with an expiry time, not a lock held by a live process.
* **Why:** a crashed worker cannot release a lock, but its lease expires on its own.
* **Solves:** two workers acting on the same row (the loser changes zero rows), and recovery
  after a crash: any worker can pick up expired work, and compare-and-set guarantees exactly
  one succeeds.
* **Makes harder:** "lease expired" does not mean "nothing happened", so every recovery path
  must first ask the provider what actually happened (`reconcile_predial`). Lease lengths are
  tuning parameters (10 s dial lease, 3 s reconcile delay).

## 3. Single-use signed permits from a shared ledger

* **What:** the pacer returns only a number. The Safety Controller recomputes capacity inside a
  write transaction, counts every worker's unconsumed permits, and inserts N permit rows
  (HMAC-signed, 3 s TTL). The allocator consumes one permit per call with a compare-and-set and
  is the only code that calls `provider.dial()`.
* **Why:** the safety boundary must be structural. A check inside the same loop as the pacer can
  be bypassed or forgotten; a permit cannot be produced by the pacer at all.
* **Solves:** pacing bypassing safety, several workers each dialing a full budget against the same
  agents, replay of an old decision, and explainability: every decision is a row in `decisions`
  with the pacer's rationale and safety's inputs.
* **Makes harder:** one write transaction per decision per worker (0.8 ms at 100 agents, 24 ms
  at 10,000, measured), and unused permits occupy budget until they expire, so the TTL must stay
  short.

## 4. Two-layer safety: deterministic rules, then a pessimistic statistical bound

* **What:** model-independent gates first (freshness, provider health, dial-ratio and concurrency
  ceilings, rolling abandon-rate breaker, cold-start fallback), then a binomial overflow bound at
  the upper confidence bound of the answer rate.
* **Why:** a statistical model gives utilisation but can be wrong exactly when conditions change;
  the deterministic rules do not depend on the model.
* **Solves:** abandoned calls when the model is wrong. In the 10% to 70% answer-rate jump, the
  damage was limited to calls already in flight (12 abandons, 1.49%) before the breaker forced
  progressive mode.
* **Makes harder:** the bound is conservative by construction (it assumes all ringing calls could
  answer at once against agents free now), which leaves ~15% of agents idle at a 20% answer rate.

## 5. Webhook inbox table plus a monotonic call state machine

* **What:** webhooks are stored with `INSERT OR IGNORE` keyed on `(provider, event_id)`; each event
  is processed in one transaction; call states only move up a fixed rank and terminal states
  absorb every later event.
* **Why:** providers deliver duplicates and out-of-order events and give no reliable sequence
  number across retries, so order cannot be restored; the state machine has to be correct
  regardless of order.
* **Solves:** duplicate events (dropped at the inbox or a no-op), out-of-order events (stale ones
  ignored), and crashes during processing (rollback; the event stays available to any worker).
* **Makes harder:** processed events are deleted after an hour, so a provider redelivery older
  than that is accepted again and relies on the state machine to be a no-op; processing is serial
  per worker, so high event rates need partitioning by `call_id`.

## 6. Answer rate measured on resolved dial cohorts

* **What:** answer-rate statistics use calls dialed more than 30 s ago, so every outcome is known,
  plus a lower bound from answers already seen among the most recent dials.
* **Why:** an earlier "last N outcomes" estimator read 55-60% at a true 30%: answers resolve in
  ~8 s and no-answers in ~25 s, so the newest outcomes are mostly answers.
* **Solves:** a biased estimate that caused unnecessary cancellations of ringing calls; found in
  the simulator.
* **Makes harder:** a 30 s lag before a regime change is fully visible; the lower bound covers
  sudden increases.

## 7. Python, a virtual clock, and the same code in simulation and tests

* **What:** Python standard library only. Every component takes `now` as a parameter instead of
  reading the wall clock, and the simulator runs the production worker, safety, allocator, event
  and recovery code against mock providers.
* **Why:** the code being simulated is the code that ships; there is no separate model of the
  dialer to drift out of date.
* **Solves:** fast and reproducible runs (20 simulated minutes in ~15 s from a seed; the
  simulator also injects a seeded id generator, because random `uuid4` ids made ties on equal
  timestamps break differently from run to run), and
  deterministic tests of timing-dependent behaviour such as the 2 s abandon timer and lease
  expiry.
* **Makes harder:** scenario runs are single threaded, so real thread interleavings are covered
  only by the concurrency tests and the load test; provider latency is modelled as event delay,
  not as a blocking call that occupies a worker.

## 8. Per-worker circuit breakers

* **What:** an in-memory circuit breaker per provider in each worker, with a router that fails
  over in priority order.
* **Why:** no shared write on every dial outcome; each worker detects an outage from its own
  failures within a few calls.
* **Solves:** piling calls onto a failing provider; safety rejects new dials while no breaker
  admits calls and caps approvals at the probe count while half-open.
* **Makes harder:** workers disagree for a few calls at the start of an outage. A breaker shared
  through the database would converge faster at the cost of a write per dial outcome.

## 9. Trigger-maintained counters for the safety snapshot

* **What:** `agent_counts` and `call_counts` tables kept up to date by database triggers on
  `agents` and `calls`. The safety snapshot reads these rows instead of counting the tables.
* **Why:** the snapshot runs under the global write lock every second on every worker, so its
  cost directly limits throughput. Triggers update the counters in the same transaction as the
  state change, so they cannot drift from the tables, and no application code path can forget
  to update them.
* **Solves:** decision cost growing with the number of agents and calls. Together with fixing an
  unindexed sort found by profiling, the snapshot went from 36 ms to 1.4 ms at 10,000 agents.
  The invariant checker compares the counters with real counts in every test and simulation run.
* **Makes harder:** every state change pays an extra write (about a quarter of raw allocation
  throughput), and on Postgres a single counter row would become a contended hot spot, so the
  counters must be split per agent pool before many workers write concurrently.

## 10. Pacer and safety share one overflow model

* **What:** the predictive pacer uses the same binomial overflow bound as the Safety Controller
  (`stats.py`), on the same count of free agents and with the same dial-ratio ceiling, which is
  defined once in `SafetyConfig` and published to the pacer in the snapshot, but with
  the expected answer rate and the 3% regulatory target. Safety applies it with an upper confidence
  bound and a 1.5% target.
* **Why:** an earlier pacer used an expected-value formula and credited agents about to free up.
  It asked for roughly 25 times what safety approved, so its output carried no useful signal and
  a pacer 5x too aggressive produced identical results.
* **Solves:** the pacer's request now means something: safety allows 84-90% of the pacer's
  in-flight target, and the remaining difference is exactly the safety margin.
  Utilisation, connects and abandons were unchanged in every scenario, because safety was
  already the binding limit.
* **Makes harder:** the pacer no longer looks ahead to agents about to free up, so a time-aware
  model would have to be added to both sides consistently. The two sides share code, so a bug in
  `stats.py` affects both; the safety gates that do not use the model still bound the damage.

## 11. Calling hours are a Safety Controller gate

* **What:** `SafetyConfig.calling_window` (08:00-19:00 local) is checked inside the safety
  transaction. Outside it every request is rejected; inside it, permits expire at the earlier of
  3 s and the window's end.
* **Why:** dialing outside permitted hours is a compliance failure of the same kind as an
  abandoned call, so it belongs with the other rules the pacer cannot override, and the permit
  check already sits on the only path to `provider.dial()`.
* **Solves:** one gate covers progressive and predictive, every worker, and permits issued
  seconds before closing; no call is placed after 19:00 even if a worker holds a permit.
* **Makes harder:** the window is per campaign. Borrower-requested hours (allowed outside the
  window) and other time zones need a per-borrower window applied at borrower selection, and the
  simulator and tests must set a clock origin (`clock_offset_s`) so time 0 falls inside the window.
