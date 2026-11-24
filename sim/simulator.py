"""Discrete-time simulator. Uses the real dialer code (DB, pacing, safety, allocator, events,
recovery) on a virtual clock, so 20 simulated minutes run in seconds."""
import json
import os
import random
import tempfile
from dataclasses import dataclass, field

from smartdialer import repo
from smartdialer.allocator import WorkerCrash
from smartdialer.config import DialerConfig, SafetyConfig
from smartdialer.db import Db
from smartdialer.health import ProviderRouter
from smartdialer.invariants import check as check_invariants
from smartdialer.pacing import PredictivePacer, ProgressivePacer
from smartdialer.providers import PROVIDER_A, PROVIDER_B, MockProvider, Population

SECRET = b"sim-secret"


@dataclass
class Scenario:
    name: str
    answer_rate: object = 0.3          # float or f(t)
    talk_time: object = 120.0          # float or f(t)
    mode: str = "PREDICTIVE"
    providers: tuple = ("A",)
    agents: int = 100
    duration: float = 1200.0
    workers: int = 1
    wrap_s: tuple = (5.0, 15.0)
    faults: list = field(default_factory=list)   # (t, kind, arg)
    aggressiveness: float = 1.0
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    seed: int = 7
    check_every: float = 60.0


class Sim:
    def __init__(self, sc: Scenario):
        self.sc = sc
        self.rng = random.Random(sc.seed)
        fd, self.path = tempfile.mkstemp(suffix=".db", prefix="sim-")
        os.close(fd)
        self.db = Db(self.path, fast=True)
        self.pop = Population(sc.answer_rate, sc.talk_time)
        cfgs = {"A": PROVIDER_A, "B": PROVIDER_B}
        self.providers = [MockProvider(cfgs[n], self.pop, seed=sc.seed + i)
                          for i, n in enumerate(sc.providers)]
        self.router = ProviderRouter(self.providers)
        self.now = 0.0
        repo.add_agents(self.db, [f"ag-{i:05d}" for i in range(sc.agents)], 0.0)
        n_b = int(sc.agents * sc.duration / 8) + 1000
        repo.add_borrowers(self.db, [(f"b-{i:06d}", f"+9190000{i:05d}", self.rng.randint(0, 3))
                                     for i in range(n_b)])
        self.workers = []
        from smartdialer.worker import DialerWorker
        for i in range(sc.workers):
            pacer = (PredictivePacer(aggressiveness=sc.aggressiveness) if sc.mode == "PREDICTIVE"
                     else ProgressivePacer())
            w = DialerWorker(f"w{i + 1}", self.db, self.router, pacer, SECRET,
                             DialerConfig(), sc.safety, crash_hook=self._hook(f"w{i + 1}"))
            self.workers.append(w)
        self.alive = {w.id: True for w in self.workers}
        self.crash_at = {}            # worker id -> crash point name
        self._held = []
        for p in self.providers:
            p.set_sink(self._webhook)
        self.wrap_target = {}
        self.samples = []
        self.decisions = []
        self.events_log = []
        self.invariant_errors = []
        self.faults = sorted(sc.faults, key=lambda f: f[0])

    # webhooks land on whichever worker is up (load balancer)
    def _webhook(self, ev):
        for w in self.workers:
            if self.alive[w.id]:
                for held in self._held:
                    w.events.ingest(held, self.now)
                self._held = []
                w.events.ingest(ev, self.now)
                return
        self._held.append(ev)     # no worker up: provider keeps retrying the webhook

    def _hook(self, wid):
        def hook(point, **kw):
            if self.crash_at.get(wid) == point:
                self.crash_at.pop(wid)
                raise WorkerCrash(f"{wid} crashed at {point} ({kw.get('call_id', '')[:8]})")
        return hook

    def _fault(self, kind, arg):
        t = self.now
        if kind == "outage":
            name, on = arg
            for p in self.providers:
                if p.name == name:
                    p.set_outage(on)
        elif kind == "agents_drop":
            ids = [r["id"] for r in self.db.q("SELECT id FROM agents WHERE state!='OFFLINE'")]
            for aid in self.rng.sample(ids, min(arg, len(ids))):
                repo.agent_logout(self.db, aid, t)
        elif kind == "agents_add":
            ids = [r["id"] for r in self.db.q("SELECT id FROM agents WHERE state='OFFLINE'")]
            for aid in ids[:arg]:
                repo.agent_ready(self.db, aid, t)
        elif kind == "crash_worker":
            wid, point = arg
            self.crash_at[wid] = point
        elif kind == "restart_worker":
            # a fresh process: new id, no in-memory state, same database
            from smartdialer.worker import DialerWorker
            old = next(w for w in self.workers if w.id == arg)
            nid = arg + "r"
            w = DialerWorker(nid, self.db, self.router, old.pacer, SECRET, DialerConfig(),
                             self.sc.safety, crash_hook=self._hook(nid))
            self.workers.append(w)
            self.alive[nid] = True
        self.events_log.append({"t": t, "fault": kind, "arg": str(arg)})

    def _agents_step(self):
        t = self.now
        for r in self.db.q("SELECT id, state_since FROM agents WHERE state='WRAP_UP'"):
            key = (r["id"], r["state_since"])
            if key not in self.wrap_target:
                self.wrap_target[key] = r["state_since"] + self.rng.uniform(*self.sc.wrap_s)
            if t >= self.wrap_target[key]:
                repo.agent_ready(self.db, r["id"], t)
                del self.wrap_target[key]
        if int(t * 2) % 10 == 0:
            with self.db.tx() as c:
                c.execute("UPDATE agents SET last_heartbeat=? WHERE state!='OFFLINE'", (t,))

    def _sample(self, decisions_this_tick):
        a = {r[0]: r[1] for r in self.db.q("SELECT state, COUNT(*) FROM agents GROUP BY state")}
        staffed = sum(v for k, v in a.items() if k not in ("OFFLINE",))
        c = {r[0]: r[1] for r in self.db.q(
            "SELECT state, COUNT(*) FROM calls WHERE state IN ('QUEUED','RESERVED','INITIATED',"
            "'RINGING','ANSWERED','CONNECTED') GROUP BY state")}
        tot = self.db.one(
            "SELECT COUNT(provider_call_id) dialed, SUM(connected_at IS NOT NULL) conn, "
            "SUM(state='ABANDONED') ab, SUM(net_outcome='ANSWER') ans FROM calls")
        req = sum(d[1].calls for d in decisions_this_tick)
        apr = sum(d[2].approved for d in decisions_this_tick)
        last = decisions_this_tick[-1][2] if decisions_this_tick else None
        self.samples.append({
            "t": self.now, "staffed": staffed, **{f"agent_{k}": v for k, v in a.items()},
            "util_talk": a.get("CONNECTED", 0) / staffed if staffed else 0.0,
            "util_busy": (a.get("CONNECTED", 0) + a.get("WRAP_UP", 0)) / staffed if staffed else 0.0,
            "in_flight": sum(c.get(k, 0) for k in ("QUEUED", "RESERVED", "INITIATED", "RINGING")),
            "answered_waiting": c.get("ANSWERED", 0),
            "dialed_total": tot["dialed"] or 0, "connected_total": tot["conn"] or 0,
            "abandoned_total": tot["ab"] or 0, "answered_total": tot["ans"] or 0,
            "requested": req, "approved": apr,
            "verdict": last.verdict if last else "",
            "mode": last.mode.value if last else "",
            "p_hat": (decisions_this_tick[-1][1].rationale.get("p_hat") if decisions_this_tick else None),
            "p_hi": last.numbers.get("p_hi") if last else None,
            "cancel": sum(d[2].cancel_unbacked for d in decisions_this_tick),
            "providers": json.dumps(self.router.states(self.now)),
        })

    def run(self, dt=0.5, sample_every=5.0, progress=False):
        sc = self.sc
        steps = int(sc.duration / dt)
        tick_decisions = []
        next_check = sc.check_every
        for i in range(steps + 1):
            self.now = t = round(i * dt, 3)
            while self.faults and self.faults[0][0] <= t:
                _, kind, arg = self.faults.pop(0)
                self._fault(kind, arg)
            for p in self.providers:
                p.pump(t)
            self._agents_step()
            for w in self.workers:
                if not self.alive[w.id]:
                    continue
                try:
                    w.service(t)
                    if abs(t - round(t)) < 1e-9:
                        d = w.pace(t)
                        tick_decisions.append((w.id, w.last_request, d))
                        self.decisions.append({
                            "t": t, "worker": w.id, "requested": w.last_request.calls,
                            "req_mode": w.last_request.mode.value, "approved": d.approved,
                            "mode": d.mode.value, "verdict": d.verdict, "cancel": d.cancel_unbacked,
                            "reasons": d.reasons, "numbers": d.numbers,
                            "pacing": w.last_request.rationale})
                except WorkerCrash as e:
                    self.alive[w.id] = False
                    self.events_log.append({"t": t, "crash": str(e)})
            if t >= next_check:
                next_check += sc.check_every
                errs = check_invariants(self.db)
                if errs:
                    self.invariant_errors.append({"t": t, "errors": errs[:5]})
            if abs((t / sample_every) - round(t / sample_every)) < 1e-9:
                self._sample(tick_decisions)
                tick_decisions = []
            if progress and i % 400 == 0:
                print(f"  {sc.name} t={t:.0f}s", flush=True)
        return self.summary()

    def summary(self):
        db = self.db
        warm = 120.0      # skip ramp-up when averaging utilisation
        ss = [s for s in self.samples if s["t"] >= warm] or self.samples
        tot = db.one(
            "SELECT COUNT(*) created, COUNT(provider_call_id) initiated, "
            "SUM(net_outcome='ANSWER') answered, SUM(connected_at IS NOT NULL) connected, "
            "SUM(state='ABANDONED') abandoned, SUM(state='CANCELLED') cancelled, "
            "SUM(state='FAILED') failed, "
            "SUM(connected_at IS NOT NULL AND connected_at - answered_at > 2.0) late_connect, "
            "AVG(CASE WHEN connected_at IS NOT NULL THEN connected_at - answered_at END) wait "
            "FROM calls")
        conn, ab = tot["connected"] or 0, tot["abandoned"] or 0
        verdicts = {}
        for d in self.decisions:
            verdicts[d["verdict"]] = verdicts.get(d["verdict"], 0) + 1
        ev = {}
        for w in self.workers:
            for k, v in w.events.outcomes.items():
                ev[k] = ev.get(k, 0) + v
        alloc = {}
        for w in self.workers:
            for k, v in w.allocator.stats.items():
                alloc[k] = alloc.get(k, 0) + v
        house = {}
        for w in self.workers:
            for k, v in w.house.stats.items():
                house[k] = house.get(k, 0) + v
        dup_inbox = sum(p.stats["events"] for p in self.providers) - db.one(
            "SELECT COUNT(*) FROM call_events")[0]
        return {
            "scenario": self.sc.name, "mode": self.sc.mode, "providers": ",".join(self.sc.providers),
            "agents": self.sc.agents, "duration_s": self.sc.duration, "workers": self.sc.workers,
            "util_talk": round(sum(s["util_talk"] for s in ss) / len(ss), 3),
            "util_busy": round(sum(s["util_busy"] for s in ss) / len(ss), 3),
            "calls_created": tot["created"], "calls_initiated": tot["initiated"],
            "answered": tot["answered"] or 0, "connected": conn, "abandoned": ab,
            "abandon_rate": round(ab / (conn + ab), 4) if conn + ab else 0.0,
            "late_connects": tot["late_connect"] or 0,
            "avg_answer_to_agent_s": round(tot["wait"] or 0.0, 2),
            "cancelled": tot["cancelled"] or 0, "failed": tot["failed"] or 0,
            "connects_per_agent_hour": round(conn / self.sc.agents / (self.sc.duration / 3600), 2),
            "safety_verdicts": verdicts, "event_outcomes": ev, "inbox_exact_dups_dropped": dup_inbox,
            "allocator": alloc, "housekeeping": house,
            "provider_trips": {n: b.trips for n, b in self.router.breakers.items()},
            "invariant_errors": self.invariant_errors, "final_invariants": check_invariants(db),
            "log": self.events_log,
        }

    def close(self):
        self.db.close()
        for ext in ("", "-wal", "-shm"):
            try:
                os.remove(self.path + ext)
            except FileNotFoundError:
                pass
