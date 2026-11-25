"""Two (or many) workers racing for the same agent / borrower."""
import multiprocessing as mp
import sqlite3
import threading
import uuid

import pytest

from smartdialer import repo
from smartdialer.db import Db
from smartdialer.models import CallState as C, Mode


def _reserve_once(db, owner, barrier, results):
    barrier.wait()
    cid = uuid.uuid4().hex
    with db.tx() as c:
        aid = repo.reserve_agent(c, cid, owner, 100.0, 1.0)
    results.append(aid)


def test_two_threads_same_agent(tmp_path):
    for round_ in range(30):
        db = Db(str(tmp_path / f"r{round_}.db"))
        repo.add_agents(db, ["only"], 0.0)
        barrier, results = threading.Barrier(2), []
        ts = [threading.Thread(target=_reserve_once, args=(db, f"w{i}", barrier, results))
              for i in range(2)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        assert sorted(results, key=str) == sorted(["only", None], key=str)
        assert db.one("SELECT state FROM agents")["state"] == "RESERVED"


def _proc_worker(path, owner, n_tries, q):
    db = Db(path)
    got_agents, got_borrowers = [], []
    for _ in range(n_tries):
        cid = uuid.uuid4().hex
        with db.tx() as c:
            aid = repo.reserve_agent(c, cid, owner, 1e9, 1.0)
            b = repo.claim_borrower(c, owner, 1.0, 1e9, cid, True)
            if aid and b:
                repo.insert_call(c, cid, b["id"], aid, Mode.PROGRESSIVE, C.RESERVED, None, owner, 1e9, 1.0)
        if aid:
            got_agents.append(aid)
        if b:
            got_borrowers.append(b["id"])
    q.put((got_agents, got_borrowers))


def test_many_processes_never_double_allocate(tmp_path):
    path = str(tmp_path / "mp.db")
    db = Db(path)
    repo.add_agents(db, [f"a{i}" for i in range(50)], 0.0)
    repo.add_borrowers(db, [(f"b{i}", "x", 0) for i in range(60)])
    q = mp.get_context("spawn").Queue()
    ps = [mp.get_context("spawn").Process(target=_proc_worker, args=(path, f"p{i}", 20, q))
          for i in range(6)]
    [p.start() for p in ps]
    res = [q.get(timeout=60) for _ in ps]
    [p.join() for p in ps]
    agents = [a for r in res for a in r[0]]
    borrowers = [b for r in res for b in r[1]]
    assert len(agents) == 50 and len(set(agents)) == 50          # each agent exactly once
    assert len(borrowers) == len(set(borrowers)) == 60            # each borrower exactly once


def test_database_backstop_rejects_second_active_call_per_agent(tmp_path):
    db = Db(str(tmp_path / "u.db"))
    with db.tx() as c:
        repo.insert_call(c, "c1", "b1", "a1", Mode.PROGRESSIVE, C.RESERVED, None, "w", 0, 0)
    with pytest.raises(sqlite3.IntegrityError):
        with db.tx() as c:
            repo.insert_call(c, "c2", "b2", "a1", Mode.PROGRESSIVE, C.RESERVED, None, "w", 0, 0)
    with pytest.raises(sqlite3.IntegrityError):
        with db.tx() as c:
            repo.insert_call(c, "c3", "b1", None, Mode.PREDICTIVE, C.QUEUED, None, "w", 0, 0)


def test_stale_cas_loses(tmp_path):
    """Worker read AVAILABLE earlier; by the time it writes, someone else reserved it."""
    db = Db(str(tmp_path / "s.db"))
    repo.add_agents(db, ["a"], 0.0)
    with db.tx() as c:
        assert repo.reserve_specific_agent(c, "a", "c1", "w1", 9, 1)
    with db.tx() as c:
        assert not repo.reserve_specific_agent(c, "a", "c2", "w2", 9, 1)


def _churn(path, seed, n, q):
    import random
    rng = random.Random(seed)
    db = Db(path)
    for _ in range(n):
        cid = uuid.uuid4().hex
        with db.tx() as c:
            aid = repo.reserve_agent(c, cid, f"p{seed}", 1e9, 1.0)
            b = repo.claim_borrower(c, f"p{seed}", 1.0, 1e9, cid, True) if aid else None
            if aid and b:
                repo.insert_call(c, cid, b["id"], aid, Mode.PROGRESSIVE, C.RESERVED, None, "w", 1e9, 1.0)
            elif aid:
                repo.agent_cas(c, aid, [repo.A.RESERVED], repo.A.AVAILABLE, 1.0, call_id=cid, set_call=None)
        if aid and b:
            end = rng.choice([C.COMPLETED, C.FAILED, C.CANCELLED])
            with db.tx() as c:
                repo.call_cas(c, cid, [C.RESERVED], end, 2.0)
                repo.release_agent_for_call(c, aid, cid, 2.0)
                repo.settle_borrower(c, b["id"], cid, "requeue", 2.0, repo.DialerConfig())
                c.execute("UPDATE borrowers SET next_attempt_at=0 WHERE id=?", (b["id"],))
    q.put(seed)


def test_counters_stay_exact_under_concurrent_writes(tmp_path):
    """Trigger-maintained counters must equal real counts after racing processes."""
    from smartdialer.invariants import check
    path = str(tmp_path / "cnt.db")
    db = Db(path)
    repo.add_agents(db, [f"a{i}" for i in range(20)], 0.0)
    repo.add_borrowers(db, [(f"b{i}", "x", 0) for i in range(40)])
    ctx = mp.get_context("spawn")
    q = ctx.Queue()
    ps = [ctx.Process(target=_churn, args=(path, i, 150, q)) for i in range(4)]
    [p.start() for p in ps]
    [q.get(timeout=120) for _ in ps]
    [p.join() for p in ps]
    assert check(db) == []
    counts = {r["state"]: r["n"] for r in db.q("SELECT state, n FROM agent_counts")}
    assert sum(counts.values()) == 20
