import itertools

from smartdialer.models import (AGENT_TRANSITIONS, CALL_TRANSITIONS, RANK, TERMINAL, AgentState,
                                CallState as C, resolve_event)


def fold(start, events):
    st, outcomes = start, []
    for typ, payload in events:
        tgt, out = resolve_event(st, typ, payload)
        outcomes.append(out)
        if tgt:
            st = tgt
    return st, outcomes


def test_triple_answered_then_completed():
    st, out = fold(C.INITIATED, [("ANSWERED", {})] * 3 + [("COMPLETED", {"answered": True})])
    assert st == C.COMPLETED
    assert out == ["applied", "duplicate", "duplicate", "applied"]


def test_completed_answered_ringing():
    st, out = fold(C.INITIATED, [("COMPLETED", {"answered": True}), ("ANSWERED", {}), ("RINGING", {})])
    assert st == C.COMPLETED
    assert out == ["applied", "after_terminal", "after_terminal"]


def test_completed_without_answer_is_failed():
    st, _ = fold(C.RINGING, [("COMPLETED", {"answered": False, "reason": "no_answer"})])
    assert st == C.FAILED


def test_completed_after_answered_counts_as_answered_even_without_flag():
    st, _ = fold(C.RINGING, [("ANSWERED", {}), ("COMPLETED", {})])
    assert st == C.COMPLETED


def test_every_ordering_of_a_normal_call_converges():
    evs = [("RINGING", {}), ("ANSWERED", {}), ("COMPLETED", {"answered": True})]
    for perm in itertools.permutations(evs):
        for dup in range(3):   # duplicate one event too
            seq = list(perm) + [perm[dup]]
            st, _ = fold(C.INITIATED, seq)
            assert st == C.COMPLETED, seq


def test_rank_never_decreases():
    for src, dsts in CALL_TRANSITIONS.items():
        for d in dsts:
            assert RANK[d] > RANK[src], (src, d)


def test_terminal_states_absorb():
    for t in TERMINAL:
        assert CALL_TRANSITIONS[t] == set()
        for typ in ("RINGING", "ANSWERED", "COMPLETED", "FAILED"):
            assert resolve_event(t, typ, {"answered": True})[0] is None


def test_agent_cannot_jump_from_connected_to_available():
    assert AgentState.AVAILABLE not in AGENT_TRANSITIONS[AgentState.CONNECTED]
