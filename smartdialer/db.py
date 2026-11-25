"""SQLite source of truth. Every state change is a conditional UPDATE inside BEGIN IMMEDIATE.

Why SQLite: one file, zero setup, real cross-process locking. The SQL maps 1:1 onto
Postgres (BEGIN IMMEDIATE -> row locks / SELECT ... FOR UPDATE SKIP LOCKED).
"""
import sqlite3
import threading
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    id TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 0,
    call_id TEXT,
    lease_owner TEXT,
    lease_until REAL,
    state_since REAL NOT NULL DEFAULT 0,
    last_heartbeat REAL NOT NULL DEFAULT 0,
    pending_offline INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_agents_state ON agents(state, state_since);

CREATE TABLE IF NOT EXISTS borrowers (
    id TEXT PRIMARY KEY,
    phone TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'PENDING',
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    provider_failures INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL DEFAULT 0,
    progressive_only INTEGER NOT NULL DEFAULT 0,
    claimed_by TEXT,
    lease_until REAL,
    last_call_id TEXT,
    version INTEGER NOT NULL DEFAULT 0
);
-- matches claim_borrower's ORDER BY, so a claim walks the index and stops at the first due row
-- instead of sorting every pending borrower (found by the load test: 2.5x slower at 1k agents)
CREATE INDEX IF NOT EXISTS ix_borrowers_ready ON borrowers(status, progressive_only DESC, priority DESC,
                                                           next_attempt_at, id);

CREATE TABLE IF NOT EXISTS calls (
    id TEXT PRIMARY KEY,
    borrower_id TEXT NOT NULL,
    agent_id TEXT,
    mode TEXT NOT NULL,
    state TEXT NOT NULL,
    provider TEXT,
    provider_call_id TEXT,
    permit_id TEXT UNIQUE,
    owner TEXT,
    lease_until REAL,
    dial_uncertain INTEGER NOT NULL DEFAULT 0,
    reconcile_attempts INTEGER NOT NULL DEFAULT 0,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    dialed_at REAL,
    answered_at REAL,
    connected_at REAL,
    bridged_at REAL,
    ended_at REAL,
    end_reason TEXT,
    net_outcome TEXT,          -- ANSWER | NOANSWER (only outcomes that reached the network)
    outcome_at REAL,
    version INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_calls_state ON calls(state);
CREATE INDEX IF NOT EXISTS ix_calls_outcome ON calls(dialed_at) WHERE net_outcome IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_calls_dialed ON calls(dialed_at);
CREATE INDEX IF NOT EXISTS ix_calls_ended ON calls(state, ended_at);
CREATE INDEX IF NOT EXISTS ix_calls_connected ON calls(connected_at) WHERE connected_at IS NOT NULL;
-- Hard invariants enforced by the database itself, independent of application logic:
-- a borrower has at most one active call, an agent is bound to at most one active call.
CREATE UNIQUE INDEX IF NOT EXISTS ux_active_call_per_borrower ON calls(borrower_id)
    WHERE state NOT IN ('COMPLETED','FAILED','CANCELLED','ABANDONED');
CREATE UNIQUE INDEX IF NOT EXISTS ux_active_call_per_agent ON calls(agent_id)
    WHERE agent_id IS NOT NULL AND state NOT IN ('COMPLETED','FAILED','CANCELLED','ABANDONED');

CREATE TABLE IF NOT EXISTS call_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    event_id TEXT NOT NULL,
    call_id TEXT NOT NULL,
    type TEXT NOT NULL,
    occurred_at REAL NOT NULL,
    received_at REAL NOT NULL,
    payload TEXT NOT NULL,
    processed INTEGER NOT NULL DEFAULT 0,
    outcome TEXT,
    UNIQUE(provider, event_id)
);
CREATE INDEX IF NOT EXISTS ix_events_pending ON call_events(processed, seq);
CREATE INDEX IF NOT EXISTS ix_events_received ON call_events(processed, received_at);

CREATE TABLE IF NOT EXISTS permits (
    id TEXT PRIMARY KEY,
    decision_id TEXT NOT NULL,
    mode TEXT NOT NULL,
    issued_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    sig TEXT NOT NULL,
    consumed_by TEXT,
    consumed_at REAL,
    revoked INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_permits_open ON permits(consumed_by, revoked, expires_at);
CREATE INDEX IF NOT EXISTS ix_permits_issued ON permits(issued_at);

CREATE TABLE IF NOT EXISTS decisions (
    id TEXT PRIMARY KEY,
    at REAL NOT NULL,
    worker TEXT,
    requested_mode TEXT,
    requested INTEGER,
    approved INTEGER,
    approved_mode TEXT,
    verdict TEXT,
    cancel_unbacked INTEGER,
    reasons TEXT,
    numbers TEXT
);

CREATE TABLE IF NOT EXISTS safety_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    forced_progressive INTEGER NOT NULL DEFAULT 0,
    forced_since REAL,
    reason TEXT
);
INSERT OR IGNORE INTO safety_state(id) VALUES (1);

-- Running counters, maintained by triggers in the same transaction as the row they count, so
-- the safety snapshot reads a handful of rows instead of scanning agents and calls.
CREATE TABLE IF NOT EXISTS agent_counts (
    state TEXT PRIMARY KEY,
    n INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS call_counts (
    state TEXT NOT NULL,
    unbacked INTEGER NOT NULL,          -- 1 when agent_id IS NULL
    n INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (state, unbacked)
);
CREATE TRIGGER IF NOT EXISTS tr_agents_ins AFTER INSERT ON agents BEGIN
    INSERT INTO agent_counts(state, n) VALUES (NEW.state, 1)
        ON CONFLICT(state) DO UPDATE SET n = n + 1;
END;
CREATE TRIGGER IF NOT EXISTS tr_agents_upd AFTER UPDATE OF state ON agents
WHEN OLD.state <> NEW.state BEGIN
    UPDATE agent_counts SET n = n - 1 WHERE state = OLD.state;
    INSERT INTO agent_counts(state, n) VALUES (NEW.state, 1)
        ON CONFLICT(state) DO UPDATE SET n = n + 1;
END;
CREATE TRIGGER IF NOT EXISTS tr_agents_del AFTER DELETE ON agents BEGIN
    UPDATE agent_counts SET n = n - 1 WHERE state = OLD.state;
END;
CREATE TRIGGER IF NOT EXISTS tr_calls_ins AFTER INSERT ON calls BEGIN
    INSERT INTO call_counts(state, unbacked, n) VALUES (NEW.state, NEW.agent_id IS NULL, 1)
        ON CONFLICT(state, unbacked) DO UPDATE SET n = n + 1;
END;
CREATE TRIGGER IF NOT EXISTS tr_calls_upd AFTER UPDATE OF state, agent_id ON calls
WHEN OLD.state <> NEW.state OR (OLD.agent_id IS NULL) <> (NEW.agent_id IS NULL) BEGIN
    UPDATE call_counts SET n = n - 1 WHERE state = OLD.state AND unbacked = (OLD.agent_id IS NULL);
    INSERT INTO call_counts(state, unbacked, n) VALUES (NEW.state, NEW.agent_id IS NULL, 1)
        ON CONFLICT(state, unbacked) DO UPDATE SET n = n + 1;
END;
CREATE TRIGGER IF NOT EXISTS tr_calls_del AFTER DELETE ON calls BEGIN
    UPDATE call_counts SET n = n - 1 WHERE state = OLD.state AND unbacked = (OLD.agent_id IS NULL);
END;

CREATE TABLE IF NOT EXISTS workers (
    id TEXT PRIMARY KEY,
    heartbeat REAL NOT NULL
);
"""

TERMINAL_SQL = "('COMPLETED','FAILED','CANCELLED','ABANDONED')"


class Db:
    def __init__(self, path: str, fast: bool = False):
        self.path = path
        self.fast = fast
        self._local = threading.local()
        self.conn().executescript(SCHEMA)
        self.rebuild_counters()

    def rebuild_counters(self):
        """Recompute the trigger-maintained counters from the tables (startup, or a database
        created before the counters existed). Normal operation never needs this."""
        with self.tx() as c:
            c.execute("DELETE FROM agent_counts")
            c.execute("INSERT INTO agent_counts(state, n) SELECT state, COUNT(*) FROM agents GROUP BY state")
            c.execute("DELETE FROM call_counts")
            c.execute("INSERT INTO call_counts(state, unbacked, n) "
                      "SELECT state, agent_id IS NULL, COUNT(*) FROM calls GROUP BY state, agent_id IS NULL")

    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "c", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=30, isolation_level=None,
                                check_same_thread=False)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA busy_timeout=30000")
            c.execute("PRAGMA synchronous=" + ("OFF" if self.fast else "NORMAL"))
            self._local.c = c
        return c

    @contextmanager
    def tx(self):
        """Write transaction. BEGIN IMMEDIATE takes the write lock up front, so the
        read-check-write inside cannot interleave with another writer."""
        c = self.conn()
        c.execute("BEGIN IMMEDIATE")
        try:
            yield c
        except BaseException:
            c.execute("ROLLBACK")
            raise
        else:
            c.execute("COMMIT")

    def q(self, sql, args=()):
        return self.conn().execute(sql, args).fetchall()

    def one(self, sql, args=()):
        return self.conn().execute(sql, args).fetchone()

    def close(self):
        c = getattr(self._local, "c", None)
        if c is not None:
            c.close()
            self._local.c = None
