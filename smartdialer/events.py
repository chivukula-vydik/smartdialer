"""Provider event inbox. Ingest = durable insert with dedupe. Process = one transaction per
event that marks it processed and applies the state change together, so a crash anywhere
leaves either both or neither."""
import json

from . import repo
from .config import DialerConfig
from .models import (EV_ANSWERED, EV_COMPLETED, EV_FAILED, TERMINAL, AgentState as A,
                     CallState as C, resolve_event)
from .providers.base import ProviderError, ProviderEvent, ProviderTimeout

NETWORK_NOANSWER = {"no_answer", "busy"}


class EventProcessor:
    def __init__(self, db, router, cfg: DialerConfig, crash_hook=None):
        self.db = db
        self.router = router
        self.cfg = cfg
        self.crash = crash_hook or (lambda point, **kw: None)
        self.outcomes = {}

    def ingest(self, ev: ProviderEvent, now) -> bool:
        """Webhook handler. Returns False for an exact duplicate (same provider event id)."""
        with self.db.tx() as c:
            return c.execute(
                "INSERT OR IGNORE INTO call_events(provider, event_id, call_id, type, occurred_at, "
                "received_at, payload) VALUES (?,?,?,?,?,?,?)",
                (ev.provider, ev.event_id, ev.call_id, ev.type, ev.occurred_at, now,
                 json.dumps(ev.payload))).rowcount == 1

    def process_pending(self, now, limit=1000):
        rows = self.db.q("SELECT seq FROM call_events WHERE processed=0 ORDER BY seq LIMIT ?", (limit,))
        n = 0
        for r in rows:
            if self.process_one(r["seq"], now):
                n += 1
        return n

    def process_one(self, seq, now):
        bridge = None
        with self.db.tx() as c:
            # claim the event; a second worker racing on the same row gets rowcount 0
            if c.execute("UPDATE call_events SET processed=1 WHERE seq=? AND processed=0",
                         (seq,)).rowcount != 1:
                return False
            ev = c.execute("SELECT * FROM call_events WHERE seq=?", (seq,)).fetchone()
            outcome, bridge = self._apply(c, ev, now)
            c.execute("UPDATE call_events SET outcome=? WHERE seq=?", (outcome, seq))
            self.crash("event_applied", seq=seq, type=ev["type"], call_id=ev["call_id"])
        self.outcomes[outcome] = self.outcomes.get(outcome, 0) + 1
        if bridge:
            self.bridge(*bridge, now)
        return True

    def _apply(self, c, ev, now):
        call = repo.get_call(c, ev["call_id"])
        if call is None:
            return "unknown_call", None
        typ, payload = ev["type"], json.loads(ev["payload"])
        cur = C(call["state"])
        reason = payload.get("reason")

        # Record facts even if the transition itself is stale: an ANSWERED that arrives after
        # COMPLETED still tells us the borrower picked up (stats need it).
        # The provider's clock may run ahead of ours. A timestamp later than our receipt time
        # would make an answered call look younger and stretch the 2 s abandon timer, so cap it.
        t_event = min(ev["occurred_at"], ev["received_at"])
        answered = typ == EV_ANSWERED or (typ == EV_COMPLETED and payload.get("answered"))
        if answered and call["net_outcome"] is None:
            c.execute("UPDATE calls SET answered_at=COALESCE(answered_at, ?), net_outcome='ANSWER', "
                      "outcome_at=? WHERE id=?", (t_event, t_event, call["id"]))
        elif (typ in (EV_COMPLETED, EV_FAILED) and reason in NETWORK_NOANSWER
              and call["net_outcome"] is None):
            c.execute("UPDATE calls SET net_outcome='NOANSWER', outcome_at=? WHERE id=?",
                      (t_event, call["id"]))

        target, outcome = resolve_event(cur, typ, payload)
        if target is None:
            return outcome, None

        if target == C.ANSWERED:
            repo.call_cas(c, call["id"], [cur], C.ANSWERED, now)
            return "applied", self._bind_agent(c, call["id"], call["agent_id"], call["provider"], now)

        if target in TERMINAL:
            fields = {"end_reason": reason or typ.lower()}
            if not repo.call_cas(c, call["id"], [cur], target, now, **fields):
                return "lost_race", None
            repo.release_agent_for_call(c, call["agent_id"], call["id"], now)
            if call["connected_at"]:
                bo = "done"
            elif answered or call["answered_at"]:
                bo = "caller_hangup"
            elif reason in NETWORK_NOANSWER:
                bo = "no_answer"
            else:
                bo = "provider"
            repo.settle_borrower(c, call["borrower_id"], call["id"], bo, now, self.cfg)
            return "applied", None

        repo.call_cas(c, call["id"], [cur], target, now)
        return "applied", None

    def _bind_agent(self, c, call_id, agent_id, provider, now):
        """Answered: attach the reserved agent (progressive) or any free one (predictive)."""
        if agent_id and repo.agent_cas(c, agent_id, [A.RESERVED, A.DIALING], A.CONNECTED, now,
                                       call_id=call_id):
            repo.call_cas(c, call_id, [C.ANSWERED], C.CONNECTED, now, connected_at=now)
            return provider, call_id, agent_id
        if agent_id:  # reserved agent vanished between dial and answer
            c.execute("UPDATE calls SET agent_id=NULL WHERE id=?", (call_id,))
        aid = repo.connect_free_agent(c, call_id, now)
        if aid:
            repo.call_cas(c, call_id, [C.ANSWERED], C.CONNECTED, now, connected_at=now, agent_id=aid)
            return provider, call_id, aid
        return None  # waits; housekeeping binds the next free agent or abandons after grace

    def bridge(self, provider, call_id, agent_id, now):
        if not provider:
            return False
        try:
            self.router.get(provider).bridge(call_id, agent_id, now)
        except ProviderError:
            return False      # call already gone; COMPLETED will follow (or the reconciler)
        except ProviderTimeout:
            return False      # retried by housekeeping while bridged_at is NULL
        with self.db.tx() as c:
            c.execute("UPDATE calls SET bridged_at=? WHERE id=? AND bridged_at IS NULL", (now, call_id))
        return True
