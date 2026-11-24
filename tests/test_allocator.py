from smartdialer import repo
from smartdialer.invariants import check
from smartdialer.models import Mode
from smartdialer.pacing import PacingRequest
from tests.test_events_and_recovery import deliver, place


def predictive_permit(stack, now):
    """Bypass cold start for unit tests by seeding resolved history."""
    from tests.test_safety import fresh, seed_outcomes
    seed_outcomes(stack.db, 100, 0.3, now)
    fresh(stack.db, now)
    d = stack.worker.safety.evaluate(PacingRequest(Mode.PREDICTIVE, 1, now), "w1", now)
    pred = [p for p in d.permits if p.mode == Mode.PREDICTIVE]
    assert d.mode == Mode.PREDICTIVE and len(pred) == 1
    return pred[0]


def test_progressive_never_exceeds_available_agents(stack):
    for t in range(1, 6):
        d = stack.worker.pace(float(t))
        active = stack.db.one("SELECT COUNT(*) n FROM calls WHERE state NOT IN "
                              "('COMPLETED','FAILED','CANCELLED','ABANDONED')")["n"]
        assert active <= 3
        assert d.approved <= 3
    assert all(c["agent_id"] for c in stack.calls())


def test_agent_disappears_after_reserve_before_dial(stack):
    def logout(point, **kw):
        if point == "after_reserve":
            aid = stack.db.one("SELECT agent_id FROM calls WHERE id=?", (kw["call_id"],))["agent_id"]
            repo.agent_logout(stack.db, aid, 1.0)
    w = stack.new_worker("w1", crash_hook=logout)
    d = w.safety.evaluate(PacingRequest(Mode.PROGRESSIVE, 1, 1), "w1", 1)
    cid = w.allocator.place(d.permits[0], 1)
    c = stack.call(cid)
    assert c["agent_id"] is None and c["cancel_requested"] == 1
    w.allocator.process_cancellations(1.1)
    c = stack.call(cid)
    assert c["state"] == "CANCELLED"
    assert (cid, "cancelled") in stack.prov.hangups
    b = stack.borrower(c["borrower_id"])
    assert b["status"] == "PENDING" and b["attempts"] == 0
    assert check(stack.db) == []


def test_agent_disappears_while_dialing_but_borrower_answers_first(stack):
    cid = place(stack)
    aid = stack.call(cid)["agent_id"]
    repo.agent_logout(stack.db, aid, 2.0)
    assert stack.agent(aid)["state"] == "OFFLINE"
    deliver(stack, stack.prov.event(cid, "ANSWERED", 2.5), 2.5)   # before we hung up
    c = stack.call(cid)
    assert c["state"] == "CONNECTED" and c["agent_id"] not in (None, aid)   # another free agent
    assert check(stack.db) == []


def test_provider_error_releases_agent_and_backs_off(stack):
    stack.prov.dial_mode = "error"
    cid = place(stack)
    c = stack.call(cid)
    assert c["state"] == "FAILED" and c["end_reason"] == "provider_error"
    assert stack.agent(c["agent_id"])["state"] == "AVAILABLE"
    b = stack.borrower(c["borrower_id"])
    assert b["provider_failures"] == 1 and b["attempts"] == 0 and b["next_attempt_at"] > 1


def test_predictive_answer_waits_then_abandons_and_flags_borrower(stack):
    for a in ("a0", "a1", "a2"):
        repo.agent_pause(stack.db, a, 0.5)
    repo.agent_ready(stack.db, "a0", 0.6)
    p = predictive_permit(stack, 1.0)
    cid = stack.worker.allocator.place(p, 1.0)
    assert stack.call(cid)["agent_id"] is None
    repo.agent_pause(stack.db, "a0", 1.5)              # the only agent walks away
    deliver(stack, stack.prov.event(cid, "ANSWERED", 3.0), 3.0)
    assert stack.call(cid)["state"] == "ANSWERED"
    stack.worker.service(4.0)
    assert stack.call(cid)["state"] == "ANSWERED"      # still inside the 2 s grace
    stack.worker.service(5.1)
    c = stack.call(cid)
    assert c["state"] == "ABANDONED"
    assert (cid, "abandon_message") in stack.prov.hangups
    b = stack.borrower(c["borrower_id"])
    assert b["progressive_only"] == 1                    # next attempt will be agent-bound
    assert check(stack.db) == []


def test_predictive_answer_gets_agent_freed_within_grace(stack):
    for a in ("a0", "a1", "a2"):
        repo.agent_pause(stack.db, a, 0.5)
    repo.agent_ready(stack.db, "a0", 0.6)
    cid = stack.worker.allocator.place(predictive_permit(stack, 1.0), 1.0)
    repo.agent_pause(stack.db, "a0", 1.5)
    deliver(stack, stack.prov.event(cid, "ANSWERED", 3.0), 3.0)
    repo.agent_ready(stack.db, "a1", 4.0)                # someone becomes free at +1 s
    stack.worker.service(4.2)
    c = stack.call(cid)
    assert c["state"] == "CONNECTED" and c["agent_id"] == "a1"


def test_previously_abandoned_borrower_only_gets_agent_bound_call(stack):
    with stack.db.tx() as c:
        c.execute("UPDATE borrowers SET progressive_only=1, priority=9 WHERE id='b5'")
        c.execute("UPDATE borrowers SET status='DONE' WHERE id!='b5'")
    p = predictive_permit(stack, 1.0)
    assert stack.worker.allocator.place(p, 1.0) is None  # predictive permit may not take b5
    # safety carved an agent-bound permit for b5 out of the same decision
    d = stack.db.one("SELECT * FROM decisions ORDER BY at DESC LIMIT 1")
    assert '"agent_bound_first": 1' in d["numbers"]
    d = stack.worker.safety.evaluate(PacingRequest(Mode.PROGRESSIVE, 1, 1.0), "w1", 1.0)
    cid = stack.worker.allocator.place(d.permits[0], 1.0)
    assert stack.call(cid)["borrower_id"] == "b5" and stack.call(cid)["agent_id"]
