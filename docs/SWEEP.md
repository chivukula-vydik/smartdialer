# Sweep: where predictive pacing pays off

Output of `python -m sim.sweep`. Same setup as `docs/RESULTS.md`: 100 agents, provider A, 20 simulated minutes per run, utilisation averaged after the first 120 s. Each cell runs progressive and predictive with the same seed. Busy = talk + wrap-up share of staffed agents. Abandon rate = abandoned / (connected + abandoned).

![sweep](results/sweep.png)

| Answer rate | Talk | Progressive busy | Predictive busy | Gain (pts) | Connects/agent-hr (prog -> pred) | Abandoned (rate) | Cancelled | Invariants |
|---|---|---|---|---|---|---|---|---|
| 10% | 60 s | 23.4% | 56.6% | +33.2 | 12.57 -> 29.34 | 0 (0.00%) | 61 | OK |
| 30% | 60 s | 51.3% | 73.4% | +22.1 | 27.0 -> 38.16 | 0 (0.00%) | 99 | OK |
| 50% | 60 s | 68.3% | 77.9% | +9.6 | 35.85 -> 40.32 | 0 (0.00%) | 23 | OK |
| 70% | 60 s | 78.8% | 82.1% | +3.3 | 41.91 -> 42.81 | 0 (0.00%) | 8 | OK |
| 10% | 120 s | 37.5% | 71.9% | +34.4 | 10.56 -> 20.22 | 0 (0.00%) | 79 | OK |
| 30% | 120 s | 69.1% | 83.3% | +14.2 | 20.19 -> 23.46 | 0 (0.00%) | 34 | OK |
| 50% | 120 s | 79.7% | 86.5% | +6.8 | 22.8 -> 24.81 | 0 (0.00%) | 4 | OK |
| 70% | 120 s | 87.2% | 88.6% | +1.4 | 25.65 -> 26.13 | 0 (0.00%) | 0 | OK |
| 10% | 240 s | 55.9% | 82.7% | +26.8 | 8.61 -> 12.75 | 0 (0.00%) | 17 | OK |
| 30% | 240 s | 82.0% | 90.0% | +8.0 | 13.02 -> 14.1 | 0 (0.00%) | 3 | OK |
| 50% | 240 s | 89.5% | 91.6% | +2.1 | 14.43 -> 14.7 | 0 (0.00%) | 2 | OK |
| 70% | 240 s | 92.8% | 93.6% | +0.8 | 15.57 -> 14.91 | 0 (0.00%) | 0 | OK |

Highest predictive abandon rate across the grid: 0.00% (answer rate 10%, talk 60 s). Breaker trip threshold: 2.5%.
