"""End-to-end: real dialer code against mock providers on a virtual clock."""
import pytest

from sim.simulator import Scenario, Sim


@pytest.mark.parametrize("sc", [
    Scenario(name="prog", mode="PROGRESSIVE", agents=30, duration=240),
    Scenario(name="pred", agents=30, duration=240),
    Scenario(name="providerB", providers=("B",), agents=30, duration=240),
    Scenario(name="3workers", workers=3, agents=30, duration=240),
    Scenario(name="outage", agents=30, duration=300,
             faults=[(120, "outage", ("A", True)), (180, "outage", ("A", False))]),
    Scenario(name="drop", agents=40, duration=240, faults=[(150, "agents_drop", 16)]),
    Scenario(name="crash", workers=2, agents=30, duration=240,
             faults=[(100, "crash_worker", ("w1", "after_dial")), (140, "restart_worker", "w1")]),
    Scenario(name="mixed-blocks", agents=30, duration=300, queue="blocks", block=30,
             segments=((0.3, 0.6, 150), (0.5, 0.25, 120), (0.2, 0.05, 60))),
], ids=lambda s: s.name)
def test_scenario_stays_consistent(sc):
    sc.check_every = 10.0
    s = Sim(sc)
    try:
        r = s.run()
    finally:
        s.close()
    assert r["invariant_errors"] == [] and r["final_invariants"] == []
    assert r["connected"] > 0
    assert r["abandon_rate"] <= 0.03


def test_progressive_has_no_unbacked_calls():
    s = Sim(Scenario(name="p", mode="PROGRESSIVE", agents=20, duration=200))
    try:
        s.run()
        n = s.db.one("SELECT COUNT(*) FROM calls WHERE mode='PREDICTIVE'")[0]
        ab = s.db.one("SELECT COUNT(*) FROM calls WHERE state='ABANDONED'")[0]
    finally:
        s.close()
    assert n == 0 and ab == 0


def test_no_dials_after_calling_window_closes():
    """t=0 is 18:57 local; the window closes at t=180 and nothing is dialed after it."""
    s = Sim(Scenario(name="close", agents=20, duration=300, start_local_h=18.95))
    try:
        r = s.run()
        last = s.db.one("SELECT MAX(dialed_at) FROM calls")[0]
        active = s.db.one("SELECT COUNT(*) FROM calls WHERE state IN "
                          "('QUEUED','RESERVED','INITIATED','RINGING','ANSWERED')")[0]
    finally:
        s.close()
    assert r["dials_outside_window"] == 0 and r["connected"] > 0
    assert last < 180 and active == 0


def test_same_seed_same_result():
    """Seeded ids make tie-breaks on equal timestamps repeatable, so a run is reproducible."""
    def go():
        s = Sim(Scenario(name="r", agents=30, duration=240, queue="blocks", block=30,
                         segments=((0.3, 0.6, 150), (0.5, 0.25, 120), (0.2, 0.05, 60)),
                         faults=[(150, "agents_drop", 10)]))
        try:
            r = s.run()
        finally:
            s.close()
        return {k: r[k] for k in ("connected", "abandoned", "cancelled", "calls_initiated",
                                  "util_busy", "safety_verdicts")}
    assert go() == go()
