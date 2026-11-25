"""Sweep answer rate x talk time, progressive vs predictive; then clustered borrower segments
by block size and seed. Writes docs/SWEEP.md, docs/results/sweep.json and docs/results/sweep.png.

    python -m sim.sweep            # 24 + 10 runs, 20 simulated minutes each (~4 min)
    python -m sim.sweep --quick    # 10 simulated minutes each
"""
import argparse
import json
import os

from .simulator import Scenario, Sim

OUT = os.path.join(os.path.dirname(__file__), "..", "docs")
RES = os.path.join(OUT, "results")

ANSWER_RATES = (0.1, 0.3, 0.5, 0.7)
TALK_TIMES = (60, 120, 240)
BLOCKS = (None, 10, 25, 100, 200)     # None = segments shuffled through the queue
SEEDS = (7, 11)
WORST_BLOCK, WORST_SEEDS = 100, tuple(range(1, 11))   # repeat the worst case across 10 seeds
RAMP = ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95", "#0d366b"]


def run_cell(p, talk, mode, dur):
    sim = Sim(Scenario(name=f"sweep-{int(p * 100)}-{talk}-{mode[:4].lower()}", answer_rate=p,
                       talk_time=talk, mode=mode, duration=dur))
    r = sim.run()
    sim.close()
    return r


def sweep(dur):
    cells = []
    for talk in TALK_TIMES:
        for p in ANSWER_RATES:
            prog = run_cell(p, talk, "PROGRESSIVE", dur)
            pred = run_cell(p, talk, "PREDICTIVE", dur)
            ok = all(not r["final_invariants"] and not r["invariant_errors"] for r in (prog, pred))
            c = {"answer_rate": p, "talk_s": talk,
                 "prog_busy": prog["util_busy"], "pred_busy": pred["util_busy"],
                 "gain_pts": round(100 * (pred["util_busy"] - prog["util_busy"]), 1),
                 "prog_cph": prog["connects_per_agent_hour"],
                 "pred_cph": pred["connects_per_agent_hour"],
                 "pred_abandoned": pred["abandoned"], "pred_abandon_rate": pred["abandon_rate"],
                 "prog_abandoned": prog["abandoned"], "pred_cancelled": pred["cancelled"],
                 "invariants_ok": ok}
            cells.append(c)
            print(f"p={p:.0%} talk={talk}s  prog={c['prog_busy']:.1%} pred={c['pred_busy']:.1%} "
                  f"gain={c['gain_pts']:+.1f}  abandon={c['pred_abandoned']} "
                  f"({c['pred_abandon_rate']:.2%})  invariants={'OK' if ok else 'FAIL'}", flush=True)
    return cells


def segment_runs(dur):
    from .run import SEGMENTS
    out = []
    for b in BLOCKS:
        for seed in SEEDS:
            sc = Scenario(name=f"seg-{b}-{seed}", segments=SEGMENTS,
                          queue="shuffled" if b is None else "blocks", block=b or 50,
                          seed=seed, duration=dur)
            sim = Sim(sc)
            r = sim.run()
            sim.close()
            ok = not r["final_invariants"] and not r["invariant_errors"]
            row = {"block": b, "seed": seed, "busy": r["util_busy"], "connected": r["connected"],
                   "abandoned": r["abandoned"], "abandon_rate": r["abandon_rate"],
                   "cancelled": r["cancelled"], "invariants_ok": ok}
            out.append(row)
            print(f"segments block={b} seed={seed}  busy={r['util_busy']:.1%} abandoned="
                  f"{r['abandoned']} ({r['abandon_rate']:.2%}) cancelled={r['cancelled']}", flush=True)
    for seed in WORST_SEEDS:
        if seed in SEEDS:
            continue
        sim = Sim(Scenario(name=f"seg-{WORST_BLOCK}-{seed}", segments=SEGMENTS, queue="blocks",
                           block=WORST_BLOCK, seed=seed, duration=dur))
        r = sim.run()
        sim.close()
        out.append({"block": WORST_BLOCK, "seed": seed, "busy": r["util_busy"],
                    "connected": r["connected"], "abandoned": r["abandoned"],
                    "abandon_rate": r["abandon_rate"], "cancelled": r["cancelled"],
                    "invariants_ok": not r["final_invariants"] and not r["invariant_errors"]})
        print(f"segments block={WORST_BLOCK} seed={seed}  abandoned={r['abandoned']} "
              f"({r['abandon_rate']:.2%})", flush=True)
    return out


def heatmap(cells, path):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.colors import LinearSegmentedColormap
        from matplotlib.patches import Rectangle
    except ImportError:
        return False
    cmap = LinearSegmentedColormap.from_list("blue", RAMP)
    top = max(1.0, max(c["gain_pts"] for c in cells))
    fig, a = plt.subplots(figsize=(9.2, 4.4))
    fig.patch.set_facecolor("#fcfcfb")
    a.set_facecolor("#fcfcfb")
    for c in cells:
        x, y = ANSWER_RATES.index(c["answer_rate"]), TALK_TIMES.index(c["talk_s"])
        v = max(0.0, c["gain_pts"]) / top
        # 2px-style surface gap between cells
        a.add_patch(Rectangle((x + 0.02, y + 0.02), 0.96, 0.96, facecolor=cmap(v),
                              edgecolor="none"))
        ink = "#ffffff" if v > 0.55 else "#0b0b0b"
        sub = "#ffffff" if v > 0.55 else "#52514e"
        a.text(x + 0.5, y + 0.58, f"{c['gain_pts']:+.1f} pts", ha="center", va="center",
               fontsize=12, color=ink, fontweight="bold")
        a.text(x + 0.5, y + 0.30,
               f"{c['prog_busy']:.0%} → {c['pred_busy']:.0%}  |  abandon {c['pred_abandon_rate']:.1%}",
               ha="center", va="center", fontsize=8, color=sub)
    a.set_xlim(0, len(ANSWER_RATES))
    a.set_ylim(0, len(TALK_TIMES))
    a.set_xticks([i + 0.5 for i in range(len(ANSWER_RATES))],
                 [f"{p:.0%}" for p in ANSWER_RATES])
    a.set_yticks([i + 0.5 for i in range(len(TALK_TIMES))], [f"{t} s" for t in TALK_TIMES])
    a.set_xlabel("Answer rate", color="#52514e")
    a.set_ylabel("Average talk time", color="#52514e")
    a.tick_params(length=0, colors="#52514e")
    for sp in a.spines.values():
        sp.set_visible(False)
    a.set_title("Predictive gain over progressive (agents busy, percentage points)",
                loc="left", fontsize=11, color="#0b0b0b")
    fig.tight_layout()
    fig.savefig(path, dpi=110)
    plt.close(fig)
    return True


def write_report(cells, segs, dur):
    L = ["# Sweep: where predictive pacing pays off", "",
         "Output of `python -m sim.sweep`. Same setup as `docs/RESULTS.md`: 100 agents, provider A, "
         f"{int(dur / 60)} simulated minutes per run, utilisation averaged after the first 120 s. "
         "Each cell runs progressive and predictive with the same seed. Busy = talk + wrap-up "
         "share of staffed agents. Abandon rate = abandoned / (connected + abandoned).", "",
         "![sweep](results/sweep.png)", "",
         "| Answer rate | Talk | Progressive busy | Predictive busy | Gain (pts) | "
         "Connects/agent-hr (prog -> pred) | Abandoned (rate) | Cancelled | Invariants |",
         "|---|---|---|---|---|---|---|---|---|"]
    for c in cells:
        L.append(f"| {c['answer_rate']:.0%} | {c['talk_s']} s | {c['prog_busy']:.1%} | "
                 f"{c['pred_busy']:.1%} | {c['gain_pts']:+.1f} | {c['prog_cph']} -> {c['pred_cph']} | "
                 f"{c['pred_abandoned']} ({c['pred_abandon_rate']:.2%}) | {c['pred_cancelled']} | "
                 f"{'OK' if c['invariants_ok'] else 'FAIL'} |")
    worst = max(cells, key=lambda c: c["pred_abandon_rate"])
    L += ["", f"Highest predictive abandon rate across the grid: {worst['pred_abandon_rate']:.2%} "
          f"(answer rate {worst['answer_rate']:.0%}, talk {worst['talk_s']} s). "
          "Breaker trip threshold: 2.5%.", "",
          "## Clustered borrower segments", "",
          "Segments: 30% of borrowers answer 60% (150 s talk), 50% answer 25% (120 s), 20% answer "
          "5% (60 s); campaign average ~31%. Shuffled: segments mixed randomly through the queue. "
          "Block N: the queue alternates runs of N borrowers from one segment, so the answer rate "
          "of consecutive dialing bursts jumps between 5% and 60%. Two seeds per row.", "",
          "| Queue | Seed | Busy | Connected | Abandoned (rate) | Cancelled | Invariants |",
          "|---|---|---|---|---|---|---|"]
    for r in segs:
        if r["block"] == WORST_BLOCK and r["seed"] not in SEEDS:
            continue
        L.append(f"| {'shuffled' if r['block'] is None else 'blocks of ' + str(r['block'])} | "
                 f"{r['seed']} | {r['busy']:.1%} | {r['connected']} | {r['abandoned']} "
                 f"({r['abandon_rate']:.2%}) | {r['cancelled']} | "
                 f"{'OK' if r['invariants_ok'] else 'FAIL'} |")
    worst = sorted((r for r in segs if r["block"] == WORST_BLOCK), key=lambda r: r["seed"])
    rates = sorted(r["abandon_rate"] for r in worst)
    over = sum(1 for r in worst if r["abandon_rate"] > 0.03)
    L += ["", f"### Blocks of {WORST_BLOCK}, {len(worst)} seeds", "",
          "| Seed | " + " | ".join(str(r["seed"]) for r in worst) + " |",
          "|---|" + "---|" * len(worst),
          "| Abandoned | " + " | ".join(str(r["abandoned"]) for r in worst) + " |",
          "| Rate | " + " | ".join(f"{r['abandon_rate']:.2%}" for r in worst) + " |", "",
          f"Median {(rates[(len(rates) - 1) // 2] + rates[len(rates) // 2]) / 2:.2%}, "
          f"maximum {rates[-1]:.2%}; {over} of {len(worst)} runs above 3%.", ""]
    with open(os.path.join(OUT, "SWEEP.md"), "w") as f:
        f.write("\n".join(L))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true")
    args = ap.parse_args()
    dur = 600 if args.quick else 1200
    os.makedirs(RES, exist_ok=True)
    cells = sweep(dur)
    segs = segment_runs(dur)
    with open(os.path.join(RES, "sweep.json"), "w") as f:
        json.dump({"grid": cells, "segments": segs}, f, indent=1)
    heatmap(cells, os.path.join(RES, "sweep.png"))
    write_report(cells, segs, dur)


if __name__ == "__main__":
    main()
