"""Housekeeping and crash recovery. Every routine is idempotent and safe to run on every
worker concurrently: each mutation is a CAS, so two reapers cannot both act on one row."""
from . import repo
from .config import DialerConfig
from .models import AgentState as A, CallState as C
from .providers.base import ProviderError, ProviderTimeout

ACTIVE = "('QUEUED','RESERVED','INITIATED','RINGING','ANSWERED','CONNECTED')"
_STATUS_MAP = {"INITIATED": C.INITIATED, "RINGING": C.RINGING}


class Housekeeper:
    def __init__(self, db, router, events, cfg: DialerConfig):
        self.db = db
        self.router = router
        self.events = events
        self.cfg = cfg
        self.stats = dict(bridged_late=0, abandoned=0, recovered=0, reconciled_failed=0,
                          stale_closed=0, leases_expired=0, presence_logouts=0)

    def run(self, now):
        self.bridge_waiting(now)
        self.abandon_expired(now)
        self.retry_unbridged(now)
        self.reconcile_predial(now)
        self.reconcile_stale(now)
        self.expire_leases(now)

    # ---- answered calls waiting for an agent ----
    def bridge_waiting(self, now):
        rows = self.db.q("SELECT id, provider FROM calls WHERE state='ANSWERED' AND agent_id IS NULL "
                         "ORDER BY answered_at")
        for r in rows:
            with self.db.tx() as c:
                aid = repo.connect_free_agent(c, r["id"], now)
                if aid is None:
                    return
                if not repo.call_cas(c, r["id"], [C.ANSWERED], C.CONNECTED, now,
                                     connected_at=now, agent_id=aid):
                    raise RuntimeError("unreachable: agent bound but call moved")  # rolls back
            self.stats["bridged_late"] += 1
            self.events.bridge(r["provider"], r["id"], aid, now)

    def abandon_expired(self, now):
        rows = self.db.q("SELECT id, provider, borrower_id FROM calls WHERE state='ANSWERED' "
                         "AND agent_id IS NULL AND answered_at<=?", (now - self.cfg.abandon_grace_s,))
        for r in rows:
            with self.db.tx() as c:
                if not repo.call_cas(c, r["id"], [C.ANSWERED], C.ABANDONED, now, end_reason="no_agent"):
                    continue
                repo.settle_borrower(c, r["borrower_id"], r["id"], "abandoned", now, self.cfg)
            self.stats["abandoned"] += 1
            try:  # play the compliance message and hang up; best effort
                self.router.get(r["provider"]).hangup(r["id"], now, reason="abandon_message")
            except (ProviderTimeout, ProviderError):
                pass

    def retry_unbridged(self, now):
        rows = self.db.q("SELECT id, provider, agent_id FROM calls WHERE state='CONNECTED' "
                         "AND bridged_at IS NULL AND connected_at<=?", (now - 0.5,))
        for r in rows:
            self.events.bridge(r["provider"], r["id"], r["agent_id"], now)

    # ---- crash / uncertain-dial recovery ----
    def reconcile_predial(self, now):
        """Calls still in QUEUED/RESERVED after their owner's lease: the worker died or the
        dial timed out. Ask the provider by our call_id before deciding anything."""
        rows = self.db.q("SELECT * FROM calls WHERE state IN ('QUEUED','RESERVED') AND lease_until<?",
                         (now,))
        for call in rows:
            if call["provider"] is None:
                self._close(call, C.FAILED, "not_dialed", "requeue", now)
                continue
            try:
                st = self.router.get(call["provider"]).get_status(call["id"], now)
            except ProviderTimeout:
                if now - call["created_at"] > self.cfg.max_uncertain_s:
                    self._hangup(call, now)
                    self._close(call, C.FAILED, "provider_lost", "provider", now)
                else:
                    with self.db.tx() as c:
                        c.execute("UPDATE calls SET lease_until=?, reconcile_attempts=reconcile_attempts+1 "
                                  "WHERE id=? AND state IN ('QUEUED','RESERVED')",
                                  (now + self.cfg.reconcile_after_s, call["id"]))
                continue
            if st is None:
                outcome = "provider" if call["dial_uncertain"] else "requeue"
                self._close(call, C.FAILED, "not_placed", outcome, now)
            elif st in ("COMPLETED", "FAILED"):
                pass  # terminal event is in the inbox (or will be); stale reconciler is the backstop
            else:
                self._adopt(call, now)

    def _adopt(self, call, now):
        """The dial did go out: move to INITIATED and keep the reserved agent."""
        with self.db.tx() as c:
            if repo.call_cas(c, call["id"], [C.QUEUED, C.RESERVED], C.INITIATED, now,
                             dial_uncertain=0, owner=None, lease_until=None):
                if call["agent_id"]:
                    repo.agent_cas(c, call["agent_id"], [A.RESERVED], A.DIALING, now,
                                   call_id=call["id"])
                self.stats["recovered"] += 1

    def reconcile_stale(self, now):
        rows = self.db.q("SELECT * FROM calls WHERE state IN ('INITIATED','RINGING','ANSWERED','CONNECTED') "
                         "AND updated_at<?", (now - self.cfg.stale_call_s,))
        for call in rows:
            try:
                st = self.router.get(call["provider"]).get_status(call["id"], now)
            except ProviderTimeout:
                if now - call["updated_at"] > 3 * self.cfg.stale_call_s:
                    self._hangup(call, now)
                    self._close(call, C.FAILED, "provider_lost", "provider", now)
                continue
            if st in (None, "COMPLETED", "FAILED"):
                if call["state"] == C.CONNECTED.value:
                    self._close(call, C.COMPLETED, "reconciled", "done", now)
                else:
                    self._close(call, C.FAILED, "reconciled", "provider", now)
                self.stats["stale_closed"] += 1
            else:
                with self.db.tx() as c:
                    c.execute("UPDATE calls SET updated_at=? WHERE id=?", (now, call["id"]))

    def expire_leases(self, now):
        with self.db.tx() as c:
            n = c.execute(
                f"""UPDATE borrowers SET status='PENDING', claimed_by=NULL, lease_until=NULL
                    WHERE status='CLAIMED' AND lease_until<? AND NOT EXISTS
                    (SELECT 1 FROM calls WHERE calls.borrower_id=borrowers.id AND calls.state IN {ACTIVE})""",
                (now,)).rowcount
            n += c.execute(
                f"""UPDATE agents SET state=CASE WHEN pending_offline=1 THEN 'OFFLINE' ELSE 'AVAILABLE' END,
                       pending_offline=0, call_id=NULL, lease_owner=NULL, lease_until=NULL,
                       state_since=?, version=version+1
                    WHERE state IN ('RESERVED','DIALING') AND COALESCE(lease_until,0)<? AND NOT EXISTS
                    (SELECT 1 FROM calls WHERE calls.agent_id=agents.id AND calls.state IN {ACTIVE})""",
                (now, now)).rowcount
        self.stats["leases_expired"] += n

    def agent_presence(self, now):
        rows = self.db.q("SELECT id FROM agents WHERE state!='OFFLINE' AND last_heartbeat<?",
                         (now - self.cfg.agent_presence_ttl_s,))
        for r in rows:
            repo.agent_logout(self.db, r["id"], now)
            self.stats["presence_logouts"] += 1

    # ---- helpers ----
    def _hangup(self, call, now):
        try:
            self.router.get(call["provider"]).hangup(call["id"], now, reason="reconcile")
        except (ProviderTimeout, ProviderError):
            pass

    def _close(self, call, state, reason, borrower_outcome, now):
        with self.db.tx() as c:
            if repo.call_cas(c, call["id"], [C(call["state"])], state, now, end_reason=reason):
                repo.release_agent_for_call(c, call["agent_id"], call["id"], now)
                repo.settle_borrower(c, call["borrower_id"], call["id"], borrower_outcome, now, self.cfg)
                if state == C.FAILED:
                    self.stats["reconciled_failed"] += 1
