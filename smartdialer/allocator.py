"""Call Allocator: turns permits into calls. The only code path that calls provider.dial()."""
import uuid

from . import repo
from .config import DialerConfig
from .models import TERMINAL, AgentState as A, CallState as C, Mode
from .providers.base import ProviderError, ProviderTimeout
from .safety import Permit, PermitVerifier


class WorkerCrash(Exception):
    """Raised by fault-injection hooks to simulate a worker dying mid-operation."""


class CallAllocator:
    def __init__(self, db, router, verifier: PermitVerifier, worker_id, cfg: DialerConfig,
                 crash_hook=None, new_id=None):
        self.db = db
        self.router = router
        self.verifier = verifier
        self.new_id = new_id or (lambda: uuid.uuid4().hex)   # injectable for reproducible simulation
        self.worker_id = worker_id
        self.cfg = cfg
        self.crash = crash_hook or (lambda point, **kw: None)
        self.stats = dict(placed=0, no_borrower=0, no_agent=0, rejected_permit=0,
                          provider_error=0, provider_timeout=0, agent_lost=0, no_provider=0)

    # ---- permits -> calls ----
    def execute(self, permits, now):
        for p in permits:
            self.place(p, now)

    def place(self, p: Permit, now):
        if not self.verifier.valid(p, now):
            self.stats["rejected_permit"] += 1
            return None
        call_id = self.new_id()
        lease = now + self.cfg.dial_lease_s
        progressive = p.mode == Mode.PROGRESSIVE
        with self.db.tx() as c:
            # single use: CAS on the permit row
            if c.execute("UPDATE permits SET consumed_by=?, consumed_at=? WHERE id=? AND sig=? "
                         "AND consumed_by IS NULL AND revoked=0 AND expires_at>?",
                         (call_id, now, p.id, p.sig, now)).rowcount != 1:
                self.stats["rejected_permit"] += 1
                return None
            agent_id = None
            if progressive:
                agent_id = repo.reserve_agent(c, call_id, self.worker_id, lease, now)
                if agent_id is None:
                    self.stats["no_agent"] += 1
                    return None        # permit is spent; nothing else changed
            b = repo.claim_borrower(c, self.worker_id, now, lease, call_id,
                                    allow_progressive_only=progressive)
            if b is None:
                self.stats["no_borrower"] += 1
                if agent_id:
                    repo.agent_cas(c, agent_id, [A.RESERVED], A.AVAILABLE, now,
                                   call_id=call_id, set_call=None)
                return None
            repo.insert_call(c, call_id, b["id"], agent_id, p.mode,
                             C.RESERVED if agent_id else C.QUEUED, p.id, self.worker_id, lease, now)
        self.crash("after_reserve", call_id=call_id)
        return self._dial(call_id, b["phone"], b["id"], agent_id, now)

    def _dial(self, call_id, phone, borrower_id, agent_id, now):
        prov = self.router.pick(now)
        if prov is None:
            self.stats["no_provider"] += 1
            self._fail_predial(call_id, borrower_id, agent_id, now, "no_provider", "requeue")
            return call_id
        with self.db.tx() as c:
            c.execute("UPDATE calls SET provider=?, dialed_at=? WHERE id=?", (prov.name, now, call_id))
        br = self.router.breakers[prov.name]
        try:
            pcid = prov.dial(call_id, phone, now)
        except ProviderTimeout:
            # Unknown outcome. Do NOT release the agent or borrower: the call may be live.
            # The reconciler asks the provider (by our call_id) and settles it.
            br.failure(now, timeout=True)
            self.stats["provider_timeout"] += 1
            with self.db.tx() as c:
                c.execute("UPDATE calls SET dial_uncertain=1, lease_until=?, updated_at=? "
                          "WHERE id=?", (now + self.cfg.reconcile_after_s, now, call_id))
            return call_id
        except ProviderError:
            br.failure(now)
            self.stats["provider_error"] += 1
            self._fail_predial(call_id, borrower_id, agent_id, now, "provider_error", "provider")
            return call_id
        br.success(now)
        self.crash("after_dial", call_id=call_id)
        self.mark_initiated(call_id, pcid, now)
        self.stats["placed"] += 1
        return call_id

    def mark_initiated(self, call_id, pcid, now):
        hang = False
        with self.db.tx() as c:
            call = repo.get_call(c, call_id)
            c.execute("UPDATE calls SET provider_call_id=?, dial_uncertain=0, owner=NULL, "
                      "lease_until=NULL WHERE id=?", (pcid, call_id))
            if C(call["state"]) in TERMINAL:
                return
            # events may already have moved it past INITIATED; CAS only from pre-dial states
            repo.call_cas(c, call_id, [C.QUEUED, C.RESERVED], C.INITIATED, now)
            if call["agent_id"]:
                ok = repo.agent_cas(c, call["agent_id"], [A.RESERVED], A.DIALING, now,
                                    call_id=call_id)
                if not ok:
                    st = c.execute("SELECT state, call_id FROM agents WHERE id=?",
                                   (call["agent_id"],)).fetchone()
                    if not (st["call_id"] == call_id and st["state"] in ("DIALING", "CONNECTED")):
                        hang = True
            elif call["mode"] == Mode.PROGRESSIVE.value or call["cancel_requested"]:
                hang = True    # agent disappeared during setup (logout detached it)
            if hang:
                c.execute("UPDATE calls SET cancel_requested=1 WHERE id=? AND state IN "
                          "('QUEUED','RESERVED','INITIATED','RINGING')", (call_id,))
        if hang:
            self.stats["agent_lost"] += 1

    def _fail_predial(self, call_id, borrower_id, agent_id, now, reason, outcome):
        with self.db.tx() as c:
            if repo.call_cas(c, call_id, [C.QUEUED, C.RESERVED], C.FAILED, now, end_reason=reason):
                repo.release_agent_for_call(c, agent_id, call_id, now)
                repo.settle_borrower(c, borrower_id, call_id, outcome, now, self.cfg)

    # ---- cancellations (agent lost, capacity shrank) ----
    def request_cancel_unbacked(self, n, now):
        """Pick the youngest unanswered predictive calls and flag them for hang-up."""
        if n <= 0:
            return 0
        with self.db.tx() as c:
            return c.execute(
                "UPDATE calls SET cancel_requested=1 WHERE id IN (SELECT id FROM calls "
                "WHERE state IN ('QUEUED','INITIATED','RINGING') AND agent_id IS NULL "
                "AND cancel_requested=0 ORDER BY state='RINGING', created_at DESC LIMIT ?)",
                (n,)).rowcount

    def process_cancellations(self, now):
        rows = self.db.q("SELECT id, provider, borrower_id, agent_id FROM calls "
                         "WHERE cancel_requested=1 AND state IN ('QUEUED','RESERVED','INITIATED','RINGING')")
        done = 0
        for r in rows:
            if r["provider"]:
                try:
                    self.router.get(r["provider"]).hangup(r["id"], now, reason="cancelled")
                except (ProviderTimeout, ProviderError):
                    continue          # try again next tick; still counted as unbacked meanwhile
            with self.db.tx() as c:
                if repo.call_cas(c, r["id"], [C.QUEUED, C.RESERVED, C.INITIATED, C.RINGING],
                                 C.CANCELLED, now, end_reason="cancelled", cancel_requested=0):
                    repo.release_agent_for_call(c, r["agent_id"], r["id"], now)
                    repo.settle_borrower(c, r["borrower_id"], r["id"], "requeue", now, self.cfg)
                    done += 1
        return done
