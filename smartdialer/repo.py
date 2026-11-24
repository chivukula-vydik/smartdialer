"""Transactional primitives. Every mutation is compare-and-set: the WHERE clause carries the
expected state, and the caller checks rowcount. Functions taking `c` must run inside Db.tx()."""
import hashlib
import sqlite3

from .config import DialerConfig
from .models import AgentState as A, BorrowerStatus as B, CallState as C, TERMINAL


# ---------- agents ----------

def add_agents(db, ids, now, state=A.AVAILABLE):
    with db.tx() as c:
        c.executemany(
            "INSERT INTO agents(id, state, state_since, last_heartbeat) VALUES (?,?,?,?)",
            [(i, state.value, now, now) for i in ids])


def reserve_agent(c, call_id, owner, lease_until, now):
    """Atomically take the longest-idle AVAILABLE agent. Returns agent id or None.

    The `AND state='AVAILABLE'` guard is the CAS. Under BEGIN IMMEDIATE only one writer
    runs at a time; on Postgres the same statement plus FOR UPDATE SKIP LOCKED in the
    subquery gives the same guarantee without serialising the whole table."""
    row = c.execute(
        """UPDATE agents SET state='RESERVED', call_id=?, lease_owner=?, lease_until=?,
               state_since=?, version=version+1
           WHERE id = (SELECT id FROM agents WHERE state='AVAILABLE'
                       ORDER BY state_since, id LIMIT 1)
             AND state='AVAILABLE'
           RETURNING id""",
        (call_id, owner, lease_until, now)).fetchone()
    return row["id"] if row else None


def reserve_specific_agent(c, agent_id, call_id, owner, lease_until, now):
    row = c.execute(
        """UPDATE agents SET state='RESERVED', call_id=?, lease_owner=?, lease_until=?,
               state_since=?, version=version+1
           WHERE id=? AND state='AVAILABLE' RETURNING id""",
        (call_id, owner, lease_until, now, agent_id)).fetchone()
    return row is not None


def connect_free_agent(c, call_id, now):
    """Predictive answer: bind any AVAILABLE agent straight to CONNECTED."""
    row = c.execute(
        """UPDATE agents SET state='CONNECTED', call_id=?, lease_owner=NULL, lease_until=NULL,
               state_since=?, version=version+1
           WHERE id = (SELECT id FROM agents WHERE state='AVAILABLE'
                       ORDER BY state_since, id LIMIT 1)
             AND state='AVAILABLE'
           RETURNING id""", (call_id, now)).fetchone()
    return row["id"] if row else None


def agent_cas(c, agent_id, from_states, to_state, now, call_id=..., set_call=...):
    """Move agent if it is in one of from_states (and bound to call_id, if given)."""
    froms = [s.value for s in from_states]
    sql = (f"UPDATE agents SET state=?, state_since=?, version=version+1"
           + (", call_id=?" if set_call is not ... else "")
           + (", lease_owner=NULL, lease_until=NULL" if to_state != A.RESERVED else "")
           + f" WHERE id=? AND state IN ({','.join('?' * len(froms))})"
           + (" AND call_id IS ?" if call_id is not ... else ""))
    args = [to_state.value, now]
    if set_call is not ...:
        args.append(set_call)
    args.append(agent_id)
    args.extend(froms)
    if call_id is not ...:
        args.append(call_id)
    return c.execute(sql, args).rowcount == 1


def release_agent_for_call(c, agent_id, call_id, now):
    """Free the agent bound to a finished call. CONNECTED -> WRAP_UP, setup states -> AVAILABLE."""
    if not agent_id:
        return None
    row = c.execute("SELECT state, call_id, pending_offline FROM agents WHERE id=?",
                    (agent_id,)).fetchone()
    if row is None or row["call_id"] != call_id:
        return None
    st = A(row["state"])
    if st == A.CONNECTED:
        agent_cas(c, agent_id, [A.CONNECTED], A.WRAP_UP, now, call_id=call_id)
        return A.WRAP_UP
    if st in (A.RESERVED, A.DIALING):
        dst = A.OFFLINE if row["pending_offline"] else A.AVAILABLE
        agent_cas(c, agent_id, [st], dst, now, call_id=call_id, set_call=None)
        if dst == A.OFFLINE:
            c.execute("UPDATE agents SET pending_offline=0 WHERE id=?", (agent_id,))
        return dst
    return None


def agent_logout(db, agent_id, now):
    """Agent disappears. Mid-setup: detach from the call and flag it for cancellation.
    Mid-conversation: finish the call first, go offline at wrap-up."""
    with db.tx() as c:
        row = c.execute("SELECT state, call_id FROM agents WHERE id=?", (agent_id,)).fetchone()
        if row is None:
            return None
        st = A(row["state"])
        if st == A.OFFLINE:
            return st
        if st == A.CONNECTED:
            c.execute("UPDATE agents SET pending_offline=1 WHERE id=?", (agent_id,))
            return st
        if st in (A.RESERVED, A.DIALING) and row["call_id"]:
            call_id = row["call_id"]
            # Detach agent; if the call has not been answered yet, we will hang it up.
            c.execute(
                f"""UPDATE calls SET agent_id=NULL,
                       cancel_requested = CASE WHEN state IN ('QUEUED','RESERVED','INITIATED','RINGING')
                                               THEN 1 ELSE cancel_requested END,
                       updated_at=?, version=version+1
                    WHERE id=? AND agent_id=? AND state NOT IN ('COMPLETED','FAILED','CANCELLED','ABANDONED')""",
                (now, call_id, agent_id))
        agent_cas(c, agent_id, [st], A.OFFLINE, now, set_call=None)
        return A.OFFLINE


def agent_ready(db, agent_id, now):
    """Agent clicks ready (after wrap-up, pause or login)."""
    with db.tx() as c:
        row = c.execute("SELECT state, pending_offline FROM agents WHERE id=?",
                        (agent_id,)).fetchone()
        if row is None:
            return False
        if row["pending_offline"] and A(row["state"]) in (A.WRAP_UP, A.PAUSED):
            c.execute("UPDATE agents SET pending_offline=0 WHERE id=?", (agent_id,))
            agent_cas(c, agent_id, [A(row["state"])], A.OFFLINE, now, set_call=None)
            return False
        return agent_cas(c, agent_id, [A.WRAP_UP, A.PAUSED, A.OFFLINE], A.AVAILABLE, now,
                         set_call=None)


def agent_pause(db, agent_id, now):
    with db.tx() as c:
        return agent_cas(c, agent_id, [A.AVAILABLE, A.WRAP_UP], A.PAUSED, now, set_call=None)


def agent_heartbeat(db, agent_id, now):
    with db.tx() as c:
        c.execute("UPDATE agents SET last_heartbeat=? WHERE id=?", (now, agent_id))


# ---------- borrowers ----------

def add_borrowers(db, rows):
    """rows: iterable of (id, phone, priority)."""
    with db.tx() as c:
        c.executemany("INSERT INTO borrowers(id, phone, priority) VALUES (?,?,?)", rows)


def claim_borrower(c, owner, now, lease_until, call_id, allow_progressive_only):
    filt = "" if allow_progressive_only else " AND progressive_only=0"
    row = c.execute(
        f"""UPDATE borrowers SET status='CLAIMED', claimed_by=?, lease_until=?, last_call_id=?,
               version=version+1
            WHERE id = (SELECT id FROM borrowers
                        WHERE status='PENDING' AND next_attempt_at<=?{filt}
                        ORDER BY progressive_only DESC, priority DESC, next_attempt_at, id LIMIT 1)
              AND status='PENDING'
            RETURNING id, phone""",
        (owner, lease_until, call_id, now)).fetchone()
    return row


def _backoff(cfg, n, key):
    base = min(cfg.provider_backoff_cap_s, cfg.provider_backoff_base_s * (2 ** max(0, n - 1)))
    jitter = int(hashlib.md5(key.encode()).hexdigest()[:4], 16) / 0xFFFF  # deterministic jitter
    return base * (0.5 + 0.5 * jitter)


def settle_borrower(c, borrower_id, call_id, outcome, now, cfg: DialerConfig):
    """Apply a call outcome to the borrower. Guarded on last_call_id so a stale or replayed
    outcome for an older call cannot touch a newer attempt."""
    row = c.execute("SELECT * FROM borrowers WHERE id=?", (borrower_id,)).fetchone()
    if row is None or row["last_call_id"] != call_id or row["status"] != B.CLAIMED.value:
        return False
    attempts, pf, prog_only = row["attempts"], row["provider_failures"], row["progressive_only"]
    status, nxt = B.PENDING.value, now
    if outcome == "done":
        status = B.DONE.value
    elif outcome in ("no_answer", "caller_hangup"):
        attempts += 1
        nxt = now + cfg.retry_noanswer_s
    elif outcome == "abandoned":
        attempts += 1
        prog_only = 1  # next attempt must have an agent already attached
        nxt = now + cfg.retry_abandon_s
    elif outcome == "provider":
        pf += 1
        nxt = now + _backoff(cfg, pf, call_id)
    elif outcome == "requeue":
        nxt = now + cfg.retry_requeue_s
    else:
        raise ValueError(outcome)
    if status == B.PENDING.value and (attempts >= row["max_attempts"]
                                      or pf >= cfg.max_provider_failures):
        status = B.EXHAUSTED.value
    c.execute("""UPDATE borrowers SET status=?, attempts=?, provider_failures=?, progressive_only=?,
                    next_attempt_at=?, claimed_by=NULL, lease_until=NULL, version=version+1
                 WHERE id=? AND last_call_id=? AND status='CLAIMED'""",
              (status, attempts, pf, prog_only, nxt, borrower_id, call_id))
    return True


# ---------- calls ----------

def insert_call(c, call_id, borrower_id, agent_id, mode, state, permit_id, owner, lease_until, now):
    c.execute(
        """INSERT INTO calls(id, borrower_id, agent_id, mode, state, permit_id, owner,
                             lease_until, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (call_id, borrower_id, agent_id, mode.value, state.value, permit_id, owner,
         lease_until, now, now))


def get_call(c, call_id):
    return c.execute("SELECT * FROM calls WHERE id=?", (call_id,)).fetchone()


def call_cas(c, call_id, from_states, to_state, now, **fields):
    """Conditional transition. Returns True only if this caller performed it."""
    froms = [s.value for s in from_states]
    sets = ["state=?", "updated_at=?", "version=version+1"]
    args = [to_state.value, now]
    if to_state in TERMINAL:
        sets.append("ended_at=COALESCE(ended_at, ?)")
        args.append(now)
    for k, v in fields.items():
        sets.append(f"{k}=?")
        args.append(v)
    args.append(call_id)
    args.extend(froms)
    sql = (f"UPDATE calls SET {', '.join(sets)} WHERE id=? "
           f"AND state IN ({','.join('?' * len(froms))})")
    return c.execute(sql, args).rowcount == 1


def is_unique_violation(e: Exception) -> bool:
    return isinstance(e, sqlite3.IntegrityError) and "UNIQUE" in str(e)
