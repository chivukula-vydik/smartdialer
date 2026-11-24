"""Bake docs/results/* into one static page: docs/dashboard.html (open it in a browser).

    python -m sim.dashboard
"""
import csv
import json
import os

from .run import OUT, RES

DESC = {
    "A": "Answer rate 20%, talk 120 s.",
    "B": "Answer rate 50%, talk 90 s.",
    "C": "Answer rate 70%, talk 180 s.",
    "D": "Answer rate 20% → 70% → 10%, talk 120 → 180 → 90 s.",
    "F1-providerB": "Provider B only: slow, timeouts, duplicate and out-of-order events. 50% / 90 s.",
    "F2-outage": "Provider A times out on everything from t=300 to t=390. 30% / 120 s.",
    "F2b-outage-failover": "Same outage, but provider B is available to fail over to.",
    "F3-agent-drop": "40 of 100 agents log out at once at t=400. 20% / 120 s.",
    "F4-worker-crash": "Two workers. w1 dies right after a dial, w2 dies after reserving, w1 comes back.",
    "F5-three-workers": "Three workers pacing the same campaign against one ledger. 30% / 120 s.",
    "F6-rate-jump": "Answer rate jumps from 10% to 70% at t=300 — the dangerous direction.",
    "F7-pacing-bug-5x": "Pacer is 5x too aggressive. Should look identical to B-pred.",
}
EXTRA_MARKS = {
    "D": [(400, "70% / 180 s"), (800, "10% / 90 s")],
    "F6-rate-jump": [(300, "answer rate 10% → 70%")],
}
VERDICT = {"APPROVED": "A", "REDUCED": "R", "REJECTED": "X", "FALLBACK_PROGRESSIVE": "F"}


def _marks(name, log):
    out = list(EXTRA_MARKS.get(name[:1] if name[0] in "ABCD" else name, []))
    for e in log:
        f, a = e.get("fault"), e.get("arg", "")
        if f == "outage":
            out.append((e["t"], "provider A " + ("down" if "True" in a else "back")))
        elif f == "agents_drop":
            out.append((e["t"], f"{a} agents leave"))
        elif f == "crash_worker":
            w, where = eval(a)  # noqa: S307 -- our own repr'd tuple from summary.json
            out.append((e["t"], f"{w} crashes ({where.replace('_', ' ')})"))
        elif f == "restart_worker":
            out.append((e["t"], f"{a} restarts"))
    return out


def _num(v):
    return float(v) if v not in ("", None) else 0.0


def load_run(r):
    name = r["scenario"]
    with open(os.path.join(RES, f"{name}.csv")) as f:
        rows = list(csv.DictReader(f))
    cols = dict(t="t", busy="util_busy", talk="agent_CONNECTED", wrap="agent_WRAP_UP",
                dial="agent_DIALING", free="agent_AVAILABLE", staffed="staffed",
                req="requested", appr="approved", ring="in_flight", aband="abandoned_total",
                p="p_hat", phi="p_hi")
    ts = {k: [round(_num(x.get(c)), 4) for x in rows] for k, c in cols.items()}
    log = []
    with open(os.path.join(RES, f"{name}.decisions.jsonl")) as f:
        for line in f:
            d = json.loads(line)
            n = d["numbers"]
            log.append([d["t"], d["worker"], d["requested"], d["approved"], VERDICT.get(d["verdict"], "?"),
                        d["cancel"], n.get("free_agents"), n.get("unbacked_in_flight"), n.get("p_hi"),
                        "; ".join(d["reasons"])])
    keep = ("mode", "providers", "agents", "workers", "duration_s", "util_busy", "util_talk",
            "calls_initiated", "answered", "connected", "abandoned", "abandon_rate", "cancelled",
            "failed", "connects_per_agent_hour", "safety_verdicts", "late_connects")
    s = {k: r[k] for k in keep}
    s["ok"] = not r["invariant_errors"] and not r["final_invariants"]
    key = name if name.startswith("F") else name[0]
    return dict(name=name, desc=DESC.get(key, ""), marks=_marks(name, r["log"]), s=s, ts=ts, log=log)


def main():
    with open(os.path.join(RES, "summary.json")) as f:
        runs = [load_run(r) for r in json.load(f)]
    data = json.dumps(runs, separators=(",", ":")).replace("</", "<\\/")
    with open(os.path.join(os.path.dirname(__file__), "dashboard.html"), encoding="utf-8") as f:
        page = f.read().replace("/*DATA*/null", data)
    out = os.path.join(OUT, "dashboard.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(page)
    print(f"wrote {os.path.normpath(out)} ({len(page) // 1024} KB)")


if __name__ == "__main__":
    main()
