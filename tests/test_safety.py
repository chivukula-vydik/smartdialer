import dataclasses
import random
import threading

import pytest

from smartdialer import repo
from smartdialer.config import SafetyConfig
from smartdialer.health import ProviderRouter
from smartdialer.models import Mode
from smartdialer.pacing import PacingRequest
from smartdialer.safety import (FALLBACK, REJECTED, Permit, PermitVerifier, SafetyController,
                                expected_overflow, max_safe_inflight, wilson_upper)
from tests.conftest import SECRET, TEST_SAFETY


def seed_outcomes(db, n, answer_rate, now, connected_share=1.0, abandoned=0):
    """Fake history: n resolved calls dialed >30s ago."""
    rng = random.Random(1)
    with db.tx() as c:
        for i in range(n):
            ans = rng.random() < answer_rate
            t = now - 60 - i * 0.1
            c.execute("INSERT INTO calls(id, borrower_id, mode, state, created_at, updated_at, "
                      "dialed_at, answered_at, connected_at, ended_at, net_outcome, outcome_at, provider_call_id) "
                      "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (f"h{i}", f"hb{i}", "PREDICTIVE", "COMPLETED" if ans else "FAILED", t, t, t,
                       t + 5 if ans else None, t + 5 if ans else None, t + 50,
                       "ANSWER" if ans else "NOANSWER", t + 5, "x"))
        for i in range(abandoned):
            t = now - 30
            c.execute("INSERT INTO calls(id, borrower_id, mode, state, created_at, updated_at, ended_at) "
                      "VALUES (?,?,?,?,?,?,?)", (f"ab{i}", f"abb{i}", "PREDICTIVE", "ABANDONED", t, t, t))


def fresh(db, now):
    with db.tx() as c:
        c.execute("UPDATE agents SET last_heartbeat=?", (now,))


def ctl(db, router=None, **over):
    return SafetyController(db, dataclasses.replace(TEST_SAFETY, **over), SECRET, router)


def test_stats_helpers():
    assert wilson_upper(20, 100, 1.645) > 0.2
    rng = random.Random(0)
    m, p, cap = 30, 0.4, 10
    mc = sum(max(0, sum(rng.random() < p for _ in range(m)) - cap) for _ in range(20000)) / 20000
    over, _ = expected_overflow(m, p, cap)
    assert abs(over - mc) < 0.05
    big, _ = expected_overflow(5000, 0.2, 900)     # normal-approx branch
    assert big > 0
    ms = [max_safe_inflight(0.3, c, 0.015, 10 * c) for c in range(0, 40)]
    assert ms == sorted(ms) and ms[0] == 0 and all(m >= c for c, m in enumerate(ms))


def test_cold_start_falls_back_to_progressive(stack):
    d = ctl(stack.db).evaluate(PacingRequest(Mode.PREDICTIVE, 500, 10), "w", 10)
    assert d.verdict == FALLBACK and d.mode == Mode.PROGRESSIVE
    assert d.approved == 3                                   # = free agents
    assert all(p.mode == Mode.PROGRESSIVE for p in d.permits)


def test_huge_request_is_bounded(stack):
    repo.add_agents(stack.db, [f"x{i}" for i in range(17)], 0.0)   # 20 free agents
    seed_outcomes(stack.db, 400, 0.3, 100)
    fresh(stack.db, 100)
    d = ctl(stack.db).evaluate(PacingRequest(Mode.PREDICTIVE, 10**6, 100), "w", 100)
    assert d.mode == Mode.PREDICTIVE
    assert 20 <= d.approved <= 4 * 20
    assert d.numbers["exp_overflow"] <= 0.015 * d.numbers["exp_connects"] + 1e-9
    assert any("overflow bound" in r or "hard cap" in r for r in d.reasons)


def test_stale_request_rejected(stack):
    d = ctl(stack.db).evaluate(PacingRequest(Mode.PROGRESSIVE, 3, 1.0), "w", 10.0)
    assert d.verdict == REJECTED and d.approved == 0


def test_open_circuit_rejects(stack):
    r = ProviderRouter([stack.prov])
    for _ in range(3):
        r.breakers["M"].failure(1.0, timeout=True)
    d = ctl(stack.db, r).evaluate(PacingRequest(Mode.PROGRESSIVE, 3, 1.0), "w", 1.0)
    assert d.verdict == REJECTED and "circuit open" in d.reasons[0]
    d = ctl(stack.db, r).evaluate(PacingRequest(Mode.PROGRESSIVE, 3, 12.0), "w", 12.0)
    assert d.approved == 2                                   # half-open: probe dials only


def test_abandon_breaker_trips_and_resets(stack):
    repo.add_agents(stack.db, [f"x{i}" for i in range(17)], 0.0)
    seed_outcomes(stack.db, 400, 0.5, 100, abandoned=10)       # ~200 connects, 10 abandons ~ 4.8%
    fresh(stack.db, 100)
    c = ctl(stack.db)
    d = c.evaluate(PacingRequest(Mode.PREDICTIVE, 50, 100), "w", 100)
    assert d.verdict == FALLBACK and any("TRIP" in r for r in d.reasons)
    assert d.approved <= 20
    # still high: stays forced even after cooldown
    fresh(stack.db, 200)
    d = c.evaluate(PacingRequest(Mode.PREDICTIVE, 50, 200), "w", 200)
    assert d.verdict == FALLBACK
    # abandons age out of the window -> resets after cooldown
    with stack.db.tx() as cx:
        cx.execute("UPDATE calls SET ended_at=-5000 WHERE state='ABANDONED'")
    fresh(stack.db, 300)
    d = c.evaluate(PacingRequest(Mode.PREDICTIVE, 50, 300), "w", 300)
    assert any("RESET" in r for r in d.reasons) and d.mode == Mode.PREDICTIVE


def test_concurrent_workers_share_one_budget(stack):
    """3 workers each ask for 3 progressive calls at the same instant; 3 agents exist."""
    out, barrier = [], threading.Barrier(3)

    def go(i):
        barrier.wait()
        d = ctl(stack.db).evaluate(PacingRequest(Mode.PROGRESSIVE, 3, 1.0), f"w{i}", 1.0)
        out.append(d.approved)
    ts = [threading.Thread(target=go, args=(i,)) for i in range(3)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sum(out) == 3


def test_permits_are_single_use_signed_and_expiring(stack):
    w = stack.worker
    d = w.safety.evaluate(PacingRequest(Mode.PROGRESSIVE, 2, 1.0), "w1", 1.0)
    p1, p2 = d.permits
    assert w.allocator.place(p1, 1.0) is not None
    assert w.allocator.place(p1, 1.0) is None                  # reuse
    forged = Permit("f" * 32, p2.decision_id, Mode.PREDICTIVE, p2.expires_at, p2.sig)
    assert not PermitVerifier(SECRET).valid(forged, 1.0)
    assert w.allocator.place(forged, 1.0) is None              # forged
    upgraded = dataclasses.replace(p2, mode=Mode.PREDICTIVE)
    assert w.allocator.place(upgraded, 1.0) is None            # tampered mode
    assert w.allocator.place(p2, 1.0 + 10) is None             # expired
    assert w.allocator.stats["rejected_permit"] == 4
    assert len(stack.calls()) == 1


def test_safety_config_is_immutable():
    with pytest.raises(dataclasses.FrozenInstanceError):
        SafetyConfig().max_dial_ratio = 100


def test_pacer_cannot_reach_provider_or_safety():
    from smartdialer.pacing import PredictivePacer, ProgressivePacer
    for pacer in (PredictivePacer(), ProgressivePacer()):
        assert not any(hasattr(pacer, a) for a in ("db", "router", "provider", "safety", "allocator"))


def test_agent_drop_cancels_excess_ringing(stack):
    """40 of 100 agents disappear: the next decision cancels unbacked calls beyond the bound."""
    repo.add_agents(stack.db, [f"x{i}" for i in range(97)], 0.0)
    repo.add_borrowers(stack.db, [(f"bb{i}", "x", 0) for i in range(1000)])
    seed_outcomes(stack.db, 400, 0.2, 100)
    fresh(stack.db, 101)
    w = stack.new_worker("w", pacer=__import__("smartdialer.pacing", fromlist=["x"]).PredictivePacer())
    d = w.safety.evaluate(PacingRequest(Mode.PREDICTIVE, 10**6, 100), "w", 100)
    w.allocator.execute(d.permits, 100)
    ringing = stack.db.one("SELECT COUNT(*) n FROM calls WHERE state='INITIATED' AND agent_id IS NULL")["n"]
    assert ringing > 100
    for r in stack.db.q("SELECT id FROM agents WHERE state='AVAILABLE' LIMIT 40"):
        repo.agent_logout(stack.db, r["id"], 101)
    d2 = w.pace(101.5)
    assert d2.cancel_unbacked > 0 and any("agents left" in r for r in d2.reasons)
    left = stack.db.one("SELECT COUNT(*) n FROM calls WHERE state IN ('INITIATED','RINGING') "
                        "AND agent_id IS NULL")["n"]
    assert left <= d2.numbers["free_agents"] * 4


def test_pacer_and_safety_share_the_model_pacer_never_below_safety(stack):
    """Same overflow model on the same snapshot: the pacer (mean answer rate, 3% target) always
    asks for at least what safety (upper bound, 1.5%) approves, and not wildly more."""
    from smartdialer.ledger import read_snapshot
    from smartdialer.pacing import PredictivePacer
    repo.add_agents(stack.db, [f"x{i}" for i in range(37)], 0.0)      # 40 agents
    repo.add_borrowers(stack.db, [(f"bb{i}", "x", 0) for i in range(500)])
    for ar in (0.2, 0.5, 0.7):
        with stack.db.tx() as c:
            c.execute("DELETE FROM calls"); c.execute("DELETE FROM permits")
        stack.db.rebuild_counters()
        seed_outcomes(stack.db, 400, ar, 100)
        fresh(stack.db, 100)
        snap = read_snapshot(stack.db.conn(), 100)
        req = PredictivePacer().request(snap)
        d = ctl(stack.db).evaluate(req, "w", 100)
        assert d.mode == Mode.PREDICTIVE
        assert req.calls >= d.approved > 0
        assert d.numbers["max_inflight_stat"] <= req.rationale["target_in_flight"]
        assert d.numbers["max_inflight_stat"] >= 0.6 * req.rationale["target_in_flight"]


def test_pacer_plans_within_the_ceiling_safety_publishes(stack):
    """The dial-ratio ceiling is defined once, in SafetyConfig; the worker publishes it to the
    pacer through the snapshot, so changing the config moves both."""
    from smartdialer.config import DialerConfig
    from smartdialer.pacing import PredictivePacer
    from smartdialer.worker import DialerWorker
    repo.add_agents(stack.db, [f"x{i}" for i in range(37)], 0.0)      # 40 agents
    repo.add_borrowers(stack.db, [(f"bb{i}", "x", 0) for i in range(2000)])
    seed_outcomes(stack.db, 400, 0.05, 100)       # low answer rate: the ceiling binds
    fresh(stack.db, 100)
    for ratio in (2.0, 3.0):
        scfg = dataclasses.replace(TEST_SAFETY, max_dial_ratio=ratio)
        w = DialerWorker(f"w{ratio}", stack.db, stack.router, PredictivePacer(), SECRET,
                         DialerConfig(), scfg)
        req = w.pacer.request(__import__("smartdialer.ledger", fromlist=["x"]).read_snapshot(
            stack.db.conn(), 100, max_dial_ratio=w.scfg.max_dial_ratio))
        cap = req.rationale["capacity"]
        assert cap > 0 and req.rationale["target_in_flight"] == int(ratio * cap)
        w.pace(100)
        assert w.last_request.rationale["target_in_flight"] == int(ratio * cap)


def test_no_permits_outside_calling_hours(stack):
    """08:00-19:00 local only. clock_offset_s=0 makes `now` the local seconds since midnight."""
    H = 3600
    c = ctl(stack.db, clock_offset_s=0.0)
    for t, open_ in ((8 * H - 1, False), (8 * H, True), (19 * H - 1, True), (19 * H, False),
                     (23 * H, False), (24 * H + 8 * H + 60, True)):
        fresh(stack.db, t)
        d = c.evaluate(PacingRequest(Mode.PROGRESSIVE, 1, t), "w", t)
        assert (d.approved == 1) == open_, t
        if not open_:
            assert d.verdict == REJECTED and "outside calling hours" in d.reasons


def test_permit_cannot_outlive_calling_window(stack):
    t = 19 * 3600 - 1.0                      # 18:59:59 local
    fresh(stack.db, t)
    d = ctl(stack.db, clock_offset_s=0.0).evaluate(PacingRequest(Mode.PROGRESSIVE, 1, t), "w", t)
    p = d.permits[0]
    assert p.expires_at == 19 * 3600         # capped at closing, not now + 3 s
    assert PermitVerifier(SECRET).valid(p, t + 0.5)
    assert stack.worker.allocator.place(p, 19 * 3600 + 0.5) is None
    assert stack.calls() == []
