"""Basic load test. Writes docs/LOAD.md.

    python -m loadtest.load_test            # ~1-2 min
    python -m loadtest.load_test --quick

1. Contention: P worker processes reserve agent + claim borrower + create call, then finish
   the call, as fast as they can, against one database. Measures throughput, transaction
   latency, and checks that nothing was double-allocated.
2. Decision cost vs scale: time for one safety decision (full snapshot + bound + permits)
   at 100 / 1,000 / 10,000 agents with realistic in-flight load and history.
3. Event pipeline: ingest + process throughput for provider events.
"""
import argparse
import multiprocessing as mp
import os
import random
import statistics
import sys
import tempfile
import time
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from smartdialer import repo  # noqa: E402
from smartdialer.config import DialerConfig, SafetyConfig  # noqa: E402
from smartdialer.db import Db  # noqa: E402
from smartdialer.events import EventProcessor  # noqa: E402
from smartdialer.health import ProviderRouter  # noqa: E402
from smartdialer.invariants import check  # noqa: E402
from smartdialer.models import CallState as C, Mode  # noqa: E402
from smartdialer.pacing import PacingRequest  # noqa: E402
from smartdialer.providers.base import ProviderEvent  # noqa: E402
from smartdialer.safety import SafetyController  # noqa: E402

CFG = DialerConfig()


def pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))] if xs else 0.0


def contention_worker(path, wid, seconds, q):
    db = Db(path)
    lat, ok, empty = [], 0, 0
    end = time.time() + seconds
    while time.time() < end:
        cid = uuid.uuid4().hex
        t0 = time.perf_counter()
        with db.tx() as c:
            aid = repo.reserve_agent(c, cid, wid, 1e12, 1.0)
            b = repo.claim_borrower(c, wid, 1.0, 1e12, cid, True) if aid else None
            if aid and b:
                repo.insert_call(c, cid, b["id"], aid, Mode.PROGRESSIVE, C.RESERVED, None, wid, 1e12, 1.0)
            elif aid:
                repo.agent_cas(c, aid, [repo.A.RESERVED], repo.A.AVAILABLE, 1.0, call_id=cid, set_call=None)
        lat.append(time.perf_counter() - t0)
        if not (aid and b):
            empty += 1
            continue
        ok += 1
        t0 = time.perf_counter()
        with db.tx() as c:
            repo.call_cas(c, cid, [C.RESERVED], C.COMPLETED, 2.0, end_reason="load")
            repo.release_agent_for_call(c, aid, cid, 2.0)
            repo.settle_borrower(c, b["id"], cid, "requeue", 2.0, CFG)
            c.execute("UPDATE borrowers SET next_attempt_at=0 WHERE id=?", (b["id"],))
            c.execute("UPDATE agents SET state='AVAILABLE' WHERE id=? AND state='WRAP_UP'", (aid,))
        lat.append(time.perf_counter() - t0)
    q.put((wid, ok, empty, lat))


def contention(procs, agents, seconds):
    path = tempfile.mktemp(suffix=".db")
    db = Db(path)
    repo.add_agents(db, [f"a{i}" for i in range(agents)], 0.0)
    repo.add_borrowers(db, [(f"b{i}", "x", 0) for i in range(agents * 2)])
    ctx = mp.get_context("spawn")
    q = ctx.Queue()
    ps = [ctx.Process(target=contention_worker, args=(path, f"p{i}", seconds, q)) for i in range(procs)]
    [p.start() for p in ps]
    res = [q.get(timeout=seconds + 60) for _ in ps]
    [p.join() for p in ps]
    ok = sum(r[1] for r in res)
    lat = [x for r in res for x in r[3]]
    errs = check(db)
    db.close()
    for ext in ("", "-wal", "-shm"):
        try:
            os.remove(path + ext)
        except FileNotFoundError:
            pass
    return {"procs": procs, "agents": agents, "calls_per_s": round(ok / seconds),
            "tx_per_s": round(len(lat) / seconds), "p50_ms": round(pct(lat, .5) * 1e3, 2),
            "p99_ms": round(pct(lat, .99) * 1e3, 2), "invariant_errors": len(errs)}


def build_scaled(agents, history):
    path = tempfile.mktemp(suffix=".db")
    db = Db(path, fast=True)
    now = 10_000.0
    rng = random.Random(1)
    repo.add_agents(db, [f"a{i}" for i in range(agents)], now)
    repo.add_borrowers(db, [(f"b{i}", "x", rng.randint(0, 3)) for i in range(max(agents * 20, history))])
    states = ["CONNECTED"] * 70 + ["WRAP_UP"] * 8 + ["AVAILABLE"] * 20 + ["PAUSED"] * 2
    with db.tx() as c:
        rows = []
        for i in range(history):
            ans = rng.random() < 0.3
            t = now - 60 - (history - i) * 0.05
            rows.append((f"h{i}", f"b{i}", "PREDICTIVE", "COMPLETED" if ans else "FAILED", t, t + 40, t,
                         t + 5 if ans else None, t + 5 if ans else None, t + 40,
                         "ANSWER" if ans else "NOANSWER", t + 5, "x"))
        c.executemany("INSERT INTO calls(id, borrower_id, mode, state, created_at, updated_at, dialed_at, "
                      "answered_at, connected_at, ended_at, net_outcome, outcome_at, provider_call_id) "
                      "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        c.execute("UPDATE borrowers SET status='DONE' WHERE id IN (SELECT borrower_id FROM calls)")
        for i in range(agents):
            st = states[i % len(states)]
            c.execute("UPDATE agents SET state=?, last_heartbeat=? WHERE id=?", (st, now, f"a{i}"))
        # ringing predictive calls ~ 0.3x agents
        ring = []
        for i in range(int(agents * 0.3)):
            ring.append((f"r{i}", f"b{history + i}", "PREDICTIVE", "RINGING", now - 5, now - 5, now - 5, "x"))
        c.executemany("INSERT INTO calls(id, borrower_id, mode, state, created_at, updated_at, dialed_at, "
                      "provider_call_id) VALUES (?,?,?,?,?,?,?,?)", ring)
        c.execute("UPDATE borrowers SET status='CLAIMED', last_call_id='r'||substr(id,2) WHERE 0")
    return db, path, now


def decision_cost(agents, history, n=30):
    db, path, now = build_scaled(agents, history)
    ctl = SafetyController(db, SafetyConfig(clock_offset_s=10 * 3600), b"k", None)   # t=0 is 10:00
    lat, approved, snap = [], [], []
    from smartdialer.ledger import read_snapshot
    for i in range(n):
        t0 = time.perf_counter()
        read_snapshot(db.conn(), now + i)
        snap.append(time.perf_counter() - t0)
        t = now + i
        t0 = time.perf_counter()
        d = ctl.evaluate(PacingRequest(Mode.PREDICTIVE, agents, t), "w", t)
        lat.append(time.perf_counter() - t0)
        approved.append(d.approved)
        with db.tx() as c:   # keep permits from piling up across iterations
            c.execute("UPDATE permits SET revoked=1")
    db.close()
    for ext in ("", "-wal", "-shm"):
        try:
            os.remove(path + ext)
        except FileNotFoundError:
            pass
    return {"agents": agents, "history_calls": history, "decision_p50_ms": round(pct(lat, .5) * 1e3, 1),
            "decision_p99_ms": round(pct(lat, .99) * 1e3, 1),
            "snapshot_p50_ms": round(pct(snap, .5) * 1e3, 1),
            "permits_per_decision": round(statistics.mean(approved))}


def events_throughput(n_calls):
    path = tempfile.mktemp(suffix=".db")
    db = Db(path)
    repo.add_agents(db, [f"a{i}" for i in range(n_calls)], 0)
    repo.add_borrowers(db, [(f"b{i}", "x", 0) for i in range(n_calls)])
    with db.tx() as c:
        for i in range(n_calls):
            c.execute("UPDATE borrowers SET status='CLAIMED', last_call_id=? WHERE id=?", (f"c{i}", f"b{i}"))
            repo.insert_call(c, f"c{i}", f"b{i}", None, Mode.PREDICTIVE, C.INITIATED, None, "w", 0, 0)
    ep = EventProcessor(db, ProviderRouter([]), CFG)
    evs = []
    for i in range(n_calls):
        evs += [ProviderEvent("A", f"e{i}r", f"c{i}", "RINGING", 1, {}),
                ProviderEvent("A", f"e{i}a", f"c{i}", "ANSWERED", 2, {}),
                ProviderEvent("A", f"e{i}a", f"c{i}", "ANSWERED", 2, {}),   # exact dup
                ProviderEvent("A", f"e{i}c", f"c{i}", "COMPLETED", 3, {"answered": True})]
    t0 = time.perf_counter()
    for e in evs:
        ep.ingest(e, 3)
    t1 = time.perf_counter()
    ep.process_pending(3, limit=10**9)
    t2 = time.perf_counter()
    errs = check(db)
    db.close()
    return {"events": len(evs), "ingest_per_s": round(len(evs) / (t1 - t0)),
            "process_per_s": round((len(evs) - n_calls) / (t2 - t1)), "invariant_errors": len(errs)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true")
    a = ap.parse_args()
    secs = 3 if a.quick else 8
    L = ["# Load test", "", "Output of `python -m loadtest.load_test` on a "
         f"{os.cpu_count()}-core container, SQLite WAL, Python {sys.version.split()[0]}.", ""]
    print("contention ...", flush=True)
    rows = [contention(p, ag, secs) for p, ag in [(1, 100), (4, 100), (8, 100), (8, 1000)]]
    L += ["## 1. Allocation under contention", "",
          "Each call = 2 write transactions (reserve agent + claim borrower + insert call; then "
          "complete + release). All processes hit the same agent pool.", "",
          "| processes | agents | calls/s | tx/s | tx p50 ms | tx p99 ms | invariant errors |",
          "|---|---|---|---|---|---|---|"]
    L += [f"| {r['procs']} | {r['agents']} | {r['calls_per_s']} | {r['tx_per_s']} | {r['p50_ms']} | "
          f"{r['p99_ms']} | {r['invariant_errors']} |" for r in rows]
    print("decision cost ...", flush=True)
    sizes = [(100, 20_000), (1_000, 100_000), (10_000, 200_000)] if not a.quick else [(100, 5000), (1000, 20000)]
    rows = [decision_cost(ag, h) for ag, h in sizes]
    L += ["", "## 2. One safety decision vs scale", "",
          "Snapshot (counts by state, outcome windows, abandon window) + binomial bound + permit "
          "inserts, in one write transaction. In-flight = 0.3 x agents ringing, 20% of agents free.", "",
          "| agents | calls in history | decision p50 ms | decision p99 ms | of which snapshot p50 ms | permits/decision |",
          "|---|---|---|---|---|---|"]
    L += [f"| {r['agents']} | {r['history_calls']} | {r['decision_p50_ms']} | {r['decision_p99_ms']} | "
          f"{r['snapshot_p50_ms']} | {r['permits_per_decision']} |" for r in rows]
    print("events ...", flush=True)
    r = events_throughput(2000 if a.quick else 5000)
    L += ["", "## 3. Event pipeline", "",
          f"{r['events']} events (25% exact duplicates): ingest {r['ingest_per_s']}/s, "
          f"process {r['process_per_s']}/s, invariant errors {r['invariant_errors']}.", ""]
    out = os.path.join(os.path.dirname(__file__), "..", "docs", "LOAD.md")
    with open(out, "w") as f:
        f.write("\n".join(L))
    print("\n".join(L))


if __name__ == "__main__":
    main()
