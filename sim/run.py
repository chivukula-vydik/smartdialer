"""Run all scenarios, write docs/RESULTS.md, CSVs and charts.

    python -m sim.run            # full set (~2-3 min)
    python -m sim.run --quick    # shorter runs
"""
import argparse
import csv
import json
import os
from .simulator import Scenario, Sim

OUT = os.path.join(os.path.dirname(__file__), "..", "docs")
RES = os.path.join(OUT, "results")


def d_rate(t):
    return 0.2 if t < 400 else (0.7 if t < 800 else 0.1)


def d_talk(t):
    return 120 if t < 400 else (180 if t < 800 else 90)


def jump_rate(t):
    return 0.1 if t < 300 else 0.7


BASE = {"A": (0.2, 120), "B": (0.5, 90), "C": (0.7, 180), "D": (d_rate, d_talk)}


def scenarios(dur):
    out = []
    for key, (ar, tt) in BASE.items():
        for mode in ("PROGRESSIVE", "PREDICTIVE"):
            out.append(Scenario(name=f"{key}-{mode[:4].lower()}", answer_rate=ar, talk_time=tt,
                                mode=mode, duration=dur))
    out += [
        Scenario(name="F1-providerB", providers=("B",), answer_rate=0.5, talk_time=90, duration=dur),
        Scenario(name="F2-outage", answer_rate=0.3, talk_time=120, duration=dur,
                 faults=[(300, "outage", ("A", True)), (390, "outage", ("A", False))]),
        Scenario(name="F2b-outage-failover", providers=("A", "B"), answer_rate=0.3, talk_time=120,
                 duration=dur, faults=[(300, "outage", ("A", True)), (390, "outage", ("A", False))]),
        Scenario(name="F3-agent-drop", answer_rate=0.2, talk_time=120, duration=dur,
                 faults=[(400, "agents_drop", 40)]),
        Scenario(name="F4-worker-crash", workers=2, answer_rate=0.3, talk_time=120, duration=dur,
                 faults=[(200, "crash_worker", ("w1", "after_dial")),
                         (300, "crash_worker", ("w2", "after_reserve")),
                         (320, "restart_worker", "w1")]),
        Scenario(name="F5-three-workers", workers=3, answer_rate=0.3, talk_time=120, duration=dur),
        Scenario(name="F6-rate-jump", answer_rate=jump_rate, talk_time=120, duration=dur),
        Scenario(name="F7-pacing-bug-5x", answer_rate=0.5, talk_time=90, duration=dur,
                 aggressiveness=5.0),
    ]
    return out


def write_csv(path, rows):
    if not rows:
        return
    keys = sorted({k for r in rows for k in r})
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        w.writerows(rows)


COLORS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100"]


def chart(name, samples, decisions, faults, path):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return False
    t = [s["t"] for s in samples]
    fig, ax = plt.subplots(3, 1, figsize=(9, 7.5), sharex=True)
    fig.patch.set_facecolor("#fcfcfb")
    for a in ax:
        a.set_facecolor("#fcfcfb")
        for sp in ("top", "right"):
            a.spines[sp].set_visible(False)
        a.grid(axis="y", color="#e6e5e1", lw=0.8)
        for ft, kind, _ in faults:
            a.axvline(ft, color="#52514e", lw=1, ls=":")
    ax[0].plot(t, [100 * s["util_busy"] for s in samples], color=COLORS[0], lw=2)
    ax[0].set_ylabel("Agents busy (%)")
    ax[0].set_ylim(0, 105)
    ax[0].set_title(name, loc="left", color="#0b0b0b", fontsize=12)
    ax[1].plot(t, [s["requested"] for s in samples], color=COLORS[1], lw=2, label="pacing asked")
    ax[1].plot(t, [s["approved"] for s in samples], color=COLORS[0], lw=2, label="safety approved")
    ax[1].set_ylabel("Calls per 5 s")
    ax[1].legend(frameon=False, loc="upper right")
    ax[2].plot(t, [s["in_flight"] for s in samples], color=COLORS[2], lw=2, label="calls ringing")
    ax[2].plot(t, [s.get("agent_AVAILABLE", 0) for s in samples], color=COLORS[3], lw=2,
               label="free agents")
    ax[2].plot(t, [s["abandoned_total"] for s in samples], color="#e34948", lw=2,
               label="abandoned (cumulative)")
    ax[2].set_ylabel("Count")
    ax[2].set_xlabel("Simulated time (s)")
    ax[2].legend(frameon=False, loc="upper right")
    fig.tight_layout()
    fig.savefig(path, dpi=110)
    plt.close(fig)
    return True


def comparison_chart(rows, path):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return False
    keys = list(BASE)
    prog = [next(r for r in rows if r["scenario"] == f"{k}-prog")["util_busy"] * 100 for k in keys]
    pred = [next(r for r in rows if r["scenario"] == f"{k}-pred")["util_busy"] * 100 for k in keys]
    fig, a = plt.subplots(figsize=(8, 4))
    fig.patch.set_facecolor("#fcfcfb")
    a.set_facecolor("#fcfcfb")
    x = range(len(keys))
    w = 0.38
    b1 = a.bar([i - w / 2 - 0.01 for i in x], prog, w, color=COLORS[1], label="Progressive")
    b2 = a.bar([i + w / 2 + 0.01 for i in x], pred, w, color=COLORS[0], label="Predictive + safety")
    for bars in (b1, b2):
        for b in bars:
            a.text(b.get_x() + b.get_width() / 2, b.get_height() + 1, f"{b.get_height():.0f}",
                   ha="center", fontsize=9, color="#52514e")
    labels = {"A": "A: 20% / 120s", "B": "B: 50% / 90s", "C": "C: 70% / 180s", "D": "D: changing"}
    a.set_xticks(list(x), [labels[k] for k in keys])
    a.set_ylabel("Agents busy (talk + wrap), %")
    a.set_ylim(0, 105)
    for sp in ("top", "right"):
        a.spines[sp].set_visible(False)
    a.grid(axis="y", color="#e6e5e1", lw=0.8)
    a.set_axisbelow(True)
    a.legend(frameon=False, loc="upper left")
    fig.tight_layout()
    fig.savefig(path, dpi=110)
    plt.close(fig)
    return True


def explain(decisions, t_from):
    """Pick one REDUCED predictive decision and narrate it."""
    for d in decisions:
        if d["t"] >= t_from and d["verdict"] == "REDUCED" and d["mode"] == "PREDICTIVE" \
                and d["approved"] >= 3:
            return d
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--only", default=None)
    args = ap.parse_args()
    dur = 600 if args.quick else 1200
    os.makedirs(RES, exist_ok=True)
    rows, extras = [], {}
    for sc in scenarios(dur):
        if args.only and not sc.name.startswith(args.only):
            continue
        print(f"running {sc.name} ...", flush=True)
        sim = Sim(sc)
        r = sim.run()
        write_csv(os.path.join(RES, f"{sc.name}.csv"), sim.samples)
        with open(os.path.join(RES, f"{sc.name}.decisions.jsonl"), "w") as f:
            for d in sim.decisions:
                f.write(json.dumps(d, default=str) + "\n")
        chart(sc.name, sim.samples, sim.decisions, sc.faults, os.path.join(RES, f"{sc.name}.png"))
        extras[sc.name] = sim.decisions
        sim.close()
        rows.append(r)
        print(f"  busy={r['util_busy']:.1%} connected={r['connected']} abandoned={r['abandoned']} "
              f"rate={r['abandon_rate']:.2%} invariants={'OK' if not r['final_invariants'] and not r['invariant_errors'] else 'FAIL'}")
    path = os.path.join(RES, "summary.json")
    if args.only and os.path.exists(path):
        # --only: replace just the re-run scenarios, keep the rest
        fresh = {r["scenario"]: r for r in rows}
        with open(path) as f:
            old = json.load(f)
        rows = [fresh.pop(r["scenario"], r) for r in old] + list(fresh.values())
    with open(path, "w") as f:
        json.dump(rows, f, indent=1, default=str)
    if not args.only:
        comparison_chart(rows, os.path.join(RES, "comparison.png"))
        write_report(rows, extras)
    from .dashboard import main as dashboard   # imports this module, so import late
    dashboard()


def _row(r):
    v = r["safety_verdicts"]
    return (f"| {r['scenario']} | {r['util_busy']:.1%} | {r['util_talk']:.1%} | {r['calls_initiated']} | "
            f"{r['connected']} | {r['abandoned']} ({r['abandon_rate']:.2%}) | {r['cancelled']} | "
            f"{r['connects_per_agent_hour']} | {v.get('APPROVED', 0)}/{v.get('REDUCED', 0)}/"
            f"{v.get('REJECTED', 0)}/{v.get('FALLBACK_PROGRESSIVE', 0)} | "
            f"{'OK' if not r['final_invariants'] and not r['invariant_errors'] else 'FAIL'} |")


HDR = ("| Run | Busy | Talk | Initiated | Connected | Abandoned | Cancelled | Connects/agent-hr | "
       "Safety A/Red/Rej/FB | Invariants |\n|---|---|---|---|---|---|---|---|---|---|")


def write_report(rows, decisions):
    by = {r["scenario"]: r for r in rows}
    L = ["# Simulation results", "",
         "Generated by `python -m sim.run`. 100 agents, provider A unless stated, 1 pacing decision "
         "per worker per second, 20 simulated minutes. Utilisation averages skip the first 120 s. "
         "Busy = talk + wrap-up share of staffed agents. Safety columns count decisions: "
         "Approved / Reduced / Rejected / Fallback-to-progressive.", "",
         "## Progressive vs predictive", "", HDR]
    for k in BASE:
        for m in ("prog", "pred"):
            if f"{k}-{m}" in by:
                L.append(_row(by[f"{k}-{m}"]))
    L += ["", "![comparison](results/comparison.png)", "",
          "## Failure and stress runs (predictive mode)", "", HDR]
    for r in rows:
        if r["scenario"].startswith("F"):
            L.append(_row(r))
    L += [""]
    for r in rows:
        if r["scenario"].startswith("F"):
            L += [f"### {r['scenario']}", "",
                  f"- events: {json.dumps(r['event_outcomes'])}, exact duplicates dropped at inbox: "
                  f"{r['inbox_exact_dups_dropped']}",
                  f"- allocator: {json.dumps({k: v for k, v in r['allocator'].items() if v})}",
                  f"- housekeeping: {json.dumps({k: v for k, v in r['housekeeping'].items() if v})}",
                  f"- circuit trips: {json.dumps(r['provider_trips'])}, late connects (>2 s): "
                  f"{r['late_connects']}",
                  f"- log: {json.dumps(r['log'])}", "",
                  f"![{r['scenario']}](results/{r['scenario']}.png)", ""]
    # agent drop reaction
    if "F3-agent-drop" in decisions:
        L += ["## Agent drop: reaction trace (t = 398..404)", "", "```"]
        for d in decisions["F3-agent-drop"]:
            if 398 <= d["t"] <= 404:
                n = d["numbers"]
                L.append(f"t={d['t']:.0f} {d['verdict']:<9} asked={d['requested']:<4} approved={d['approved']:<3} "
                         f"cancel={d['cancel']:<3} free={n.get('free_agents')} unbacked={n.get('unbacked_in_flight')} "
                         f"{'; '.join(d['reasons'])}")
        L += ["```", ""]
    if "F6-rate-jump" in decisions:
        L += ["## Answer rate 10% -> 70% at t=300: safety's estimate (t = 298..312)", "", "```"]
        for d in decisions["F6-rate-jump"]:
            if 298 <= d["t"] <= 312 and d["t"] % 2 == 0:
                n = d["numbers"]
                L.append(f"t={d['t']:.0f} {d['verdict']:<9} asked={d['requested']:<4} approved={d['approved']:<3} "
                         f"p_hi={n.get('p_hi')} floor={n.get('p_floor_recent')} shift={n.get('shift')} "
                         f"free={n.get('free_agents')} unbacked={n.get('unbacked_in_flight')} cancel={d['cancel']}")
        L += ["```", ""]
    d = explain(decisions.get("B-pred", []), 400)
    if d:
        n, p = d["numbers"], d["pacing"]
        L += ["## Why this many calls? One decision, end to end", "",
              f"Scenario B, t={d['t']:.0f}s.", "",
              "Pacing engine (expected value):", "", "```",
              json.dumps(p, indent=1), "```", "",
              "Safety controller (its own numbers, read inside its transaction):", "", "```",
              json.dumps(n, indent=1), "```", "",
              f"Verdict **{d['verdict']}**: asked {d['requested']}, approved {d['approved']}. "
              f"Reasons: {'; '.join(d['reasons'])}", ""]
    with open(os.path.join(OUT, "RESULTS.md"), "w") as f:
        f.write("\n".join(L))


if __name__ == "__main__":
    main()
