"""Sweep answer rate x talk time, progressive vs predictive. Writes docs/SWEEP.md,
docs/results/sweep.json and docs/results/sweep.png.

    python -m sim.sweep            # 12 cells x 2 modes, 20 simulated minutes each (~3 min)
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


def write_report(cells, dur):
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
          "Breaker trip threshold: 2.5%.", ""]
    with open(os.path.join(OUT, "SWEEP.md"), "w") as f:
        f.write("\n".join(L))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true")
    args = ap.parse_args()
    dur = 600 if args.quick else 1200
    os.makedirs(RES, exist_ok=True)
    cells = sweep(dur)
    with open(os.path.join(RES, "sweep.json"), "w") as f:
        json.dump(cells, f, indent=1)
    heatmap(cells, os.path.join(RES, "sweep.png"))
    write_report(cells, dur)


if __name__ == "__main__":
    main()
