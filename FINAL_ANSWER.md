# Final question

> How would you build a SmartDialer that gets as much of the utilisation benefit of predictive
> dialing as possible, while retaining the deterministic safety characteristics of progressive
> dialing?

**Treat predictive as a bounded overdraft on top of progressive, not as a separate mode.**

1. **Progressive is the floor.** Dial only for agents free right now, so the worst case is zero
   abandons. The system returns to it on cold start, stale data, provider trouble, or when the
   abandon-rate breaker trips, and a borrower who was abandoned before is always dialed with an
   agent reserved.
2. **One component owns the risk.** The pacer only proposes a number. The Safety Controller
   recomputes from its own fresh snapshot with a pessimistic (Wilson upper bound) answer rate,
   counts only agents free now, and caps calls in flight so expected overflow stays under 1.5% of
   connects. It issues single-use signed permits from one ledger shared by all workers, and the
   only code that can dial requires a permit. A pacer 5x too aggressive (F7) produced the same
   result as a correct one.
3. **Limits that do not depend on any estimate:** a 4x dial-ratio ceiling, an abandon-rate breaker
   at 2.5% (below the 3% limit) that forces progressive, request freshness, and calling hours.
4. **The answer path is deterministic too:** an answered call gets a free agent or a compliance
   message within 2 s.

**Result:** at a 20% answer rate agents go from 56.5% to 82.8% busy with zero abandons. Across
the sweep, predictive adds +27 to +34 points at a 10% answer rate and +1 to +3 at 70%. After a
sudden 10% to 70% answer-rate jump the run ended at 1.49% abandons.

**Where it falls short:** the bound is conservative (~15% of agents idle at a 20% answer rate),
and one campaign-wide answer rate lags when the queue clusters very different borrowers (4.17% in
1 of 11 such runs, [docs/SWEEP.md](docs/SWEEP.md)). Next: measured answer rates per borrower group
inside the safety bound, then a time-aware bound run in shadow mode before it is trusted.
