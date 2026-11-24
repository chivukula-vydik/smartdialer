# Pacing and the Safety Controller

## The split

| | Pacing engine | Safety Controller |
|---|---|---|
| Job | "How many calls would keep agents busy?" | "How many calls are we allowed to have in flight?" |
| Inputs | Frozen `Snapshot` (may be up to 2 s old) | Fresh read inside its own write transaction |
| Answer rate | Point estimate (Beta prior + window) | Upper confidence bound (pessimistic) |
| Credits agents about to free up | Yes (expected value) | No. Only agents AVAILABLE now with a fresh heartbeat |
| Can be wrong in the aggressive direction | Yes, by design | No: this is the one that must not be |
| Output | A number + rationale | Signed single-use permits + verdict + reasons |

The pacer is allowed to be clever and wrong. The safety controller is deliberately simple,
pessimistic, and the only path to the provider.

## Predictive pacer (`smartdialer/pacing.py`)

```
p          = (prior_p*w + answers_long + answers_short) / (w + n_long + n_short)    (short window weighted 2x)
             raised to the answers already seen among calls dialed in the last 30 s
horizon    = avg ring-to-answer + 1 s
soon_free  = CONNECTED * min(1, horizon / avg_talk) + WRAP_UP * min(1, horizon / wrap)
capacity   = AVAILABLE + soon_free - answered_waiting
in_flight  = unbacked calls ringing + unconsumed predictive permits
request    = floor( aggressiveness * (capacity - in_flight * p) / p )
```

Uses current availability, connected and ringing calls, historical answer rate, setup/ring
time, average talk time and wrap time. Provider health is deliberately left to the safety
controller and the router, and provider failures never enter the answer-rate statistics (an
outage would otherwise look like "nobody answers" and make the pacer dial harder).

## Safety Controller (`smartdialer/safety.py`)

Evaluated in order, inside one `BEGIN IMMEDIATE` transaction:

| # | Gate | Kind | Effect |
|---|---|---|---|
| 1 | Rolling abandon rate (15 min) >= 2.5% (legal cap 3%) | deterministic | **Trip:** force PROGRESSIVE for all workers (state in DB) until rate <= 1.0% and >= 60 s cooldown |
| 2 | Request based on a snapshot older than 2 s | deterministic | REJECT |
| 3 | No provider breaker admits calls | deterministic | REJECT; half-open: cap at probe count |
| 4 | Fewer than 40 resolved outcomes (cold start) | deterministic | FALLBACK to progressive |
| 5 | Agents gone (stale heartbeat) | deterministic | Not counted as free |
| 6 | Capacity shock (>= 5% of agents left in 30 s, or answer-rate shift) with unbacked calls above the bound | statistical | Revoke unused permits, then **cancel** excess calls, not-yet-ringing first |
| 7 | Unbacked in flight <= 4 x free agents; <= provider concurrency | deterministic ceiling | REDUCE |
| 8 | Overflow bound (below) | statistical, pessimistic | REDUCE / REJECT |

In PROGRESSIVE (requested, cold start, or forced) the approval is
`free_agents - unbacked_in_flight - open_permits`: every in-flight call that has no agent is
treated as a certain answer, so the worst case is zero abandons. Borrowers who were abandoned
before are always served with agent-bound calls first, even in predictive mode.

### The overflow bound

Let `C` = agents AVAILABLE right now that nobody has claimed, `m` = unbacked calls in flight
after this decision, `X ~ Binomial(m, p_hi)` = how many of them answer.

```
E[abandon]  = E[(X - C)+]          (answers with no agent)
E[connect]  = E[min(X, C)]
approve the largest m with   E[abandon] <= eps * E[connect]
```

* `p_hi` = Wilson upper bound (one-sided 95%) on the last 400 resolved outcomes. A short window
  of 40 overrides it only when its *lower* bound is above the long upper bound (a real shift,
  not noise). The answers already seen among calls dialed in the last 30 s act as a floor.
* `eps` = 1.5%, shrinking to 0 as the rolling abandon rate approaches the 2.5% trip.
* The model assumes every ringing call could answer at the same instant against today's free
  agents, and that no agent frees up meanwhile. Both are false in the safe direction. That is
  why measured abandon rates sit at 0% in normal conditions.

### Why 5 calls and not 44? (real decision from scenario B, t = 401 s)

The pacer: p = 0.52, 20 free, 16.1 expected to free within the 9.3 s horizon, so capacity
36.1; 25 calls already ringing will bring ~13 answers; deficit 23.1; 23.1 / 0.52 = **44**.

The safety controller: 20 free agents (it does not credit the 16 that may free up),
p_hi = 0.568, 25 calls already ringing.

| total in flight m | E[abandon] | E[connect] | ratio |
|---|---|---|---|
| 25 (now) | 0.005 | 14.2 | 0.04% |
| **30** | **0.178** | **16.9** | **1.05%** |
| 31 | 0.282 | 17.3 | 1.63% (> 1.5%) |
| 69 (what the pacer wanted) | 19.2 | 20.0 | 96% |

So `m = 30`, room = 30 - 25 = **5** permits. Verdict REDUCED, reason
`overflow bound: p_hi=0.568, eps=1.500% allows 30 in flight for 20 agents`. Every decision is
stored in the `decisions` table (and `docs/results/*.decisions.jsonl`) with both sides'
numbers, so any "why 17 and not 10" question is a lookup.

## What the simulator showed

Full tables and charts: [RESULTS.md](RESULTS.md). 100 agents, 20 simulated minutes, provider A.

| Scenario | Progressive busy | Predictive busy | Connects/agent-hour | Abandon |
|---|---|---|---|---|
| A: 20% answer, 120 s talk | 56.5% | 82.8% | 16.0 -> 23.4 (+47%) | 0% |
| B: 50%, 90 s | 75.7% | 83.2% | 27.7 -> 30.8 (+11%) | 0% |
| C: 70%, 180 s | 90.4% | 91.8% | 19.1 -> 19.7 (+3%) | 0% |
| D: 20% -> 70% -> 10%, talk 120 -> 180 -> 90 s | 64.5% | 80.5% | 16.0 -> 20.8 (+30%) | 0% |

The gain is largest where progressive hurts most (low answer rate), and nearly zero where
agents are already saturated by long calls.

Stress runs:

* **Pacing bug (aggressiveness 5x):** identical results to B (same calls, connects, utilisation). The safety
  boundary, not the pacer, sets the rate.
* **Answer rate jumps 10% -> 70% (F6):** the dangerous direction. Calls dialed under the old
  estimate answered in a burst: 12 abandons in ~3 s. The rolling breaker tripped at t=333
  (5.0% over a 238-call window) and forced progressive for every worker; zero abandons after.
  Over the run: 1.49%, under the 3% cap. This is the layered design working: the statistical
  layer was wrong because the world changed, the deterministic layer bounded the damage.
* **Answer rate drops 70% -> 10% (D, third phase):** the safe direction. Fewer answers than
  expected means idle agents, not abandons; the estimate catches up within the 400-outcome
  window and the pacer dials harder again.
* **40% of agents vanish (F3):** the decision in the same second sees the drop, cancels 16
  unbacked calls and approves nothing until ringing calls drain to the new bound. Agents who
  were mid-call leave after their call ends, so free capacity drops by the agents that were
  actually idle (17 -> 10), not by 40 at once.

## Known weaknesses

* The bound is static in time. It leaves ~15% of agents idle at a 20% answer rate. A
  time-aware bound (answers arrive spread over the ring-time distribution; agents free at a
  measurable rate) would recover most of that. I did not ship it because it puts a prediction
  of human behaviour (talk-time) inside the safety layer. I would add it behind the same
  breaker, measured against the static bound in shadow mode first.
* Slow webhooks eat the 2 s grace: with provider B, 78 calls were connected more than 2 s after
  the borrower answered (by provider timestamp) because the ANSWERED event itself arrived late.
  We connect them anyway if an agent is free; the metric is reported, and provider choice for
  predictive campaigns should take webhook latency into account.
* Cancelling ringing calls on a capacity shock is ring-and-drop. Some regimes (for example a
  minimum 15 s ring rule) restrict it. The controller only does it on shocks, prefers calls
  not yet ringing, and the threshold is separate (6% expected abandon) from the one for new
  calls (1.5%).
