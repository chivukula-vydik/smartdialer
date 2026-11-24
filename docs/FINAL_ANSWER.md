# Final question

> How would you build a SmartDialer that gets as much of the utilization benefit of predictive
> dialing as possible, while retaining the deterministic safety characteristics of progressive
> dialing?

Treat predictive as a bounded overdraft on top of progressive, not as a different mode.

**1. Progressive is the floor, and it is deterministic.** Every call that has an agent reserved
is safe by construction. Count only agents free right now, and treat every unbacked call
already ringing as a certain answer. Under that rule the worst case is zero abandons. The
system starts there (cold start), returns there whenever anything looks wrong, and previously
abandoned borrowers are always served from it.

**2. Overdial is a budget owned by one component, not a number the algorithm emits.** The
pacing engine can be as clever as it likes; it only produces a request. A separate safety
controller decides how many *unbacked* calls may exist, from its own fresh read and its own
pessimistic answer rate (an upper confidence bound, not the mean), credits no agent that is
not free yet, and issues single-use, short-lived permits from one ledger shared by every worker.
The only code that can dial needs a permit. Nothing the pacer does can widen the budget.

**3. Bound the worst case with rules that do not depend on the model.** A hard dial-ratio
ceiling, provider-health gating, freshness checks, and a rolling abandon-rate breaker that puts
every worker back on pure progressive before the regulatory limit is reached. The statistical
layer can only spend what the deterministic layer allows; if the model is wrong (the answer rate
jumps from 10% to 70%), the damage is limited to calls already in flight, and the breaker stops
it. In the simulator that case cost 12 abandons in 3 seconds and then nothing.

**4. Make the answer path deterministic too.** An answered call atomically claims a free agent
or waits a fixed 2 s grace, then gets a compliant message and the borrower is flagged for an
agent-bound retry. Agent loss during setup cancels or rebinds the call. Capacity shocks revoke
permits and cancel calls that have not been answered yet.

**5. Spend the utilization where it exists.** The benefit of predictive is concentrated at low
answer rates. Here: at a 20% answer rate, 56% -> 83% agent busy time (+47% connects per agent
hour) with zero abandons; at 70% answer and long calls, +3%, because progressive is already near
full. A dialer that knows this can be aggressive only where it pays.

What I would do next to recover the remaining utilization (~15% idle at low answer rates): a
time-aware version of the bound (answers arrive spread over the ring-time distribution, agents
free at a measurable rate), run in shadow mode next to the static bound, promoted only if its
realised abandon rate stays under target. It would still sit behind the same breaker and
permit ledger, so being wrong about it costs a bounded number of calls, never an unbounded one.
