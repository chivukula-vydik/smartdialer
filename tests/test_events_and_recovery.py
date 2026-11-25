import pytest

from smartdialer import repo
from smartdialer.allocator import WorkerCrash
from smartdialer.invariants import check
from smartdialer.models import Mode
from smartdialer.pacing import PacingRequest


def place(stack, now=1.0, worker=None):
    w = worker or stack.worker
    d = w.safety.evaluate(PacingRequest(Mode.PROGRESSIVE, 1, now), w.id, now)
    assert d.approved == 1
    return w.allocator.place(d.permits[0], now)


def deliver(stack, ev, now, worker=None):
    w = worker or stack.worker
    w.events.ingest(ev, now)
    w.events.process_pending(now)


def test_happy_progressive_call(stack):
    cid = place(stack)
    c = stack.call(cid)
    assert c["state"] == "INITIATED"
    assert stack.agent(c["agent_id"])["state"] == "DIALING"
    p = stack.prov
    deliver(stack, p.event(cid, "RINGING", 2), 2)
    deliver(stack, p.event(cid, "ANSWERED", 5), 5)
    assert stack.call(cid)["state"] == "CONNECTED"
    assert stack.agent(c["agent_id"])["state"] == "CONNECTED"
    assert p.bridges == [(cid, c["agent_id"])]
    deliver(stack, p.event(cid, "COMPLETED", 60, answered=True), 60)
    assert stack.call(cid)["state"] == "COMPLETED"
    assert stack.agent(c["agent_id"])["state"] == "WRAP_UP"
    assert stack.borrower(c["borrower_id"])["status"] == "DONE"
    assert check(stack.db) == []


def test_exact_duplicate_events_dropped_at_inbox(stack):
    cid = place(stack)
    ev = stack.prov.event(cid, "ANSWERED", 5, eid="same")
    w = stack.worker
    assert w.events.ingest(ev, 5) is True
    assert w.events.ingest(ev, 5) is False
    assert w.events.ingest(ev, 5) is False
    w.events.process_pending(5)
    assert stack.db.one("SELECT COUNT(*) n FROM call_events")["n"] == 1
    assert len(stack.prov.bridges) == 1


def test_semantic_duplicates_with_new_ids_are_noops(stack):
    cid = place(stack)
    for i in range(3):
        deliver(stack, stack.prov.event(cid, "ANSWERED", 5 + i), 5 + i)
    deliver(stack, stack.prov.event(cid, "COMPLETED", 60, answered=True), 60)
    outs = [r["outcome"] for r in stack.db.q("SELECT outcome FROM call_events ORDER BY seq")]
    assert outs == ["applied", "stale", "stale", "applied"]   # ANSWERED again after CONNECTED is stale
    assert len(stack.prov.bridges) == 1
    v = stack.call(cid)["version"]
    deliver(stack, stack.prov.event(cid, "ANSWERED", 61), 61)
    assert stack.call(cid)["version"] == v                    # no transition after terminal


def test_out_of_order_completed_answered_ringing(stack):
    cid = place(stack)
    aid = stack.call(cid)["agent_id"]
    p = stack.prov
    deliver(stack, p.event(cid, "COMPLETED", 30, answered=True, reason="caller_hangup"), 30)
    deliver(stack, p.event(cid, "ANSWERED", 31), 31)
    deliver(stack, p.event(cid, "RINGING", 32), 32)
    c = stack.call(cid)
    assert c["state"] == "COMPLETED"
    assert c["net_outcome"] == "ANSWER"          # fact recorded even though transition was late
    assert stack.agent(aid)["state"] == "AVAILABLE"   # never talked, so no wrap-up
    b = stack.borrower(c["borrower_id"])
    assert b["status"] == "PENDING" and b["attempts"] == 1
    assert check(stack.db) == []


def test_crash_inside_answered_processing_then_completed(stack):
    """Worker crashes while applying ANSWERED (transaction rolls back). COMPLETED arrives.
    Another worker processes both in order and ends consistent."""
    cid = place(stack)
    aid = stack.call(cid)["agent_id"]

    def boom(point, **kw):
        if point == "event_applied" and kw.get("type") == "ANSWERED":
            raise WorkerCrash("died mid-transaction")
    w1 = stack.new_worker("w1-crashy", crash_hook=boom)
    w1.events.ingest(stack.prov.event(cid, "ANSWERED", 5), 5)
    with pytest.raises(WorkerCrash):
        w1.events.process_pending(5)
    assert stack.call(cid)["state"] == "INITIATED"                # rolled back
    assert stack.db.one("SELECT processed FROM call_events")["processed"] == 0
    w2 = stack.new_worker("w2")
    w2.events.ingest(stack.prov.event(cid, "COMPLETED", 50, answered=True), 50)
    w2.events.process_pending(50)
    c = stack.call(cid)
    assert c["state"] == "COMPLETED" and c["connected_at"] is not None
    assert stack.agent(aid)["state"] == "WRAP_UP"
    assert stack.borrower(c["borrower_id"])["status"] == "DONE"
    assert check(stack.db) == []


def test_crash_after_answered_commit_before_bridge(stack):
    cid = place(stack)
    stack.prov.bridge = lambda *a: (_ for _ in ()).throw(__import__(
        "smartdialer.providers.base", fromlist=["x"]).ProviderTimeout("down"))
    deliver(stack, stack.prov.event(cid, "ANSWERED", 5), 5)
    assert stack.call(cid)["state"] == "CONNECTED" and stack.call(cid)["bridged_at"] is None
    calls = []
    stack.prov.bridge = lambda call_id, agent_id, now: calls.append(call_id)
    stack.worker.house.retry_unbridged(6)
    assert calls == [cid] and stack.call(cid)["bridged_at"] == 6


def test_worker_crash_after_dial_is_adopted(stack):
    """Agent reserved > borrower reserved > call dialed > worker dies before recording it."""
    def die(point, **kw):
        if point == "after_dial":
            raise WorkerCrash("gone")
    w1 = stack.new_worker("w1", crash_hook=die)
    d = w1.safety.evaluate(PacingRequest(Mode.PROGRESSIVE, 1, 1), "w1", 1)
    with pytest.raises(WorkerCrash):
        w1.allocator.place(d.permits[0], 1)
    c = stack.db.one("SELECT * FROM calls")
    assert c["state"] == "RESERVED" and stack.agent(c["agent_id"])["state"] == "RESERVED"
    w2 = stack.new_worker("w2")
    w2.house.run(5)                      # lease not expired yet: hands off
    assert stack.call(c["id"])["state"] == "RESERVED"
    w2.house.run(20)                     # lease expired: ask provider, it knows the call
    assert stack.call(c["id"])["state"] == "INITIATED"
    assert stack.agent(c["agent_id"])["state"] == "DIALING"
    deliver(stack, stack.prov.event(c["id"], "ANSWERED", 21), 21, worker=w2)
    assert stack.call(c["id"])["state"] == "CONNECTED"
    assert check(stack.db) == []


def test_worker_crash_before_dial_releases_everything(stack):
    def die(point, **kw):
        if point == "after_reserve":
            raise WorkerCrash("gone")
    w1 = stack.new_worker("w1", crash_hook=die)
    d = w1.safety.evaluate(PacingRequest(Mode.PROGRESSIVE, 1, 1), "w1", 1)
    with pytest.raises(WorkerCrash):
        w1.allocator.place(d.permits[0], 1)
    c = stack.db.one("SELECT * FROM calls")
    stack.new_worker("w2").house.run(20)
    c2 = stack.call(c["id"])
    assert c2["state"] == "FAILED" and c2["end_reason"] == "not_dialed"
    assert stack.agent(c["agent_id"])["state"] == "AVAILABLE"
    b = stack.borrower(c["borrower_id"])
    assert b["status"] == "PENDING" and b["attempts"] == 0      # our fault, no penalty
    assert stack.prov.calls == {}


def test_dial_timeout_call_not_placed(stack):
    stack.prov.dial_mode = "timeout_lost"
    cid = place(stack)
    c = stack.call(cid)
    assert c["dial_uncertain"] == 1 and stack.agent(c["agent_id"])["state"] == "RESERVED"
    stack.worker.house.run(1 + 3.1)
    c = stack.call(cid)
    assert c["state"] == "FAILED" and c["end_reason"] == "not_placed"
    assert stack.agent(c["agent_id"])["state"] == "AVAILABLE"
    b = stack.borrower(c["borrower_id"])
    assert b["provider_failures"] == 1 and b["attempts"] == 0 and b["next_attempt_at"] > 4


def test_dial_timeout_call_actually_placed(stack):
    stack.prov.dial_mode = "timeout_placed"
    cid = place(stack)
    stack.worker.house.run(4.5)
    assert stack.call(cid)["state"] == "INITIATED"


def test_provider_unreachable_during_reconcile_gives_up_eventually(stack):
    stack.prov.dial_mode = "timeout_placed"
    cid = place(stack)
    stack.prov.status_mode = "timeout"
    for t in range(5, 30, 3):
        stack.worker.house.run(t)
        assert stack.call(cid)["state"] == "RESERVED"          # still unknown, agent held
    stack.worker.house.run(40)
    c = stack.call(cid)
    assert c["state"] == "FAILED" and c["end_reason"] == "provider_lost"
    assert stack.agent(c["agent_id"])["state"] == "AVAILABLE"
    assert (cid, "reconcile") in stack.prov.hangups            # best-effort hang up


def test_crash_after_dial_and_call_already_ended_frees_agent(stack):
    """Dialed, worker died before recording it, provider events lost, provider says the call
    is over: the reserved agent must be released, not held forever."""
    def die(point, **kw):
        if point == "after_dial":
            raise WorkerCrash("gone")
    w1 = stack.new_worker("w1", crash_hook=die)
    d = w1.safety.evaluate(PacingRequest(Mode.PROGRESSIVE, 1, 1), "w1", 1)
    with pytest.raises(WorkerCrash):
        w1.allocator.place(d.permits[0], 1)
    c = stack.db.one("SELECT * FROM calls")
    stack.prov.calls[c["id"]] = "COMPLETED"          # ended at the provider; events never arrived
    asked = []
    real = stack.prov.get_status
    stack.prov.get_status = lambda cid, now: asked.append(cid) or real(cid, now)
    w2 = stack.new_worker("w2")
    w2.house.run(20)
    c2 = stack.call(c["id"])
    assert c2["state"] == "FAILED" and c2["end_reason"] == "ended_unseen"
    assert stack.agent(c["agent_id"])["state"] == "AVAILABLE"
    b = stack.borrower(c["borrower_id"])
    assert b["status"] == "PENDING" and b["attempts"] == 0 and b["provider_failures"] == 1
    w2.house.run(21)
    assert asked == [c["id"]]                         # not polled again every second
    deliver(stack, stack.prov.event(c["id"], "ANSWERED", 3), 22, worker=w2)   # late event
    assert stack.call(c["id"])["state"] == "FAILED"
    assert check(stack.db) == []


def test_prune_removes_old_permits_and_events_only(stack):
    w = stack.worker
    d = w.safety.evaluate(PacingRequest(Mode.PROGRESSIVE, 3, 1.0), "w1", 1.0)
    cid = w.allocator.place(d.permits[0], 1.0)          # one used, two left to expire
    deliver(stack, stack.prov.event(cid, "RINGING", 2, eid="ring-1"), 2)
    d2 = w.safety.evaluate(PacingRequest(Mode.PROGRESSIVE, 1, 700.0), "w1", 700.0)
    fresh_permit = d2.permits[0].id if d2.permits else None
    n = w.house.prune(701.0)
    left = {r["id"] for r in stack.db.q("SELECT id FROM permits")}
    assert not ({p.id for p in d.permits} & left)       # used and expired permits deleted
    if fresh_permit:
        assert fresh_permit in left                     # open permit kept
    assert stack.db.one("SELECT COUNT(*) n FROM call_events")["n"] == 1   # event younger than 1 h
    w.house.prune(2 + 3601)
    assert stack.db.one("SELECT COUNT(*) n FROM call_events")["n"] == 0
    assert stack.db.one("SELECT COUNT(*) n FROM decisions")["n"] == 2    # audit log kept
    # a redelivery after pruning is accepted again but changes nothing
    v = stack.call(cid)["version"]
    deliver(stack, stack.prov.event(cid, "RINGING", 2, eid="ring-1"), 3700)
    assert stack.call(cid)["version"] == v
    assert n >= 3


def test_provider_clock_ahead_does_not_stretch_abandon_timer(stack):
    from tests.test_allocator import predictive_permit
    for a in ("a0", "a1", "a2"):
        repo.agent_pause(stack.db, a, 0.5)
    repo.agent_ready(stack.db, "a0", 0.6)
    cid = stack.worker.allocator.place(predictive_permit(stack, 1.0), 1.0)
    repo.agent_pause(stack.db, "a0", 1.5)              # nobody free when the borrower answers
    ev = stack.prov.event(cid, "ANSWERED", 3.0 + 30)   # provider clock 30 s ahead
    deliver(stack, ev, 3.0)
    assert stack.call(cid)["answered_at"] == 3.0       # capped at our receipt time
    stack.worker.service(5.1)
    assert stack.call(cid)["state"] == "ABANDONED"     # 2 s after the real answer, not 32 s
