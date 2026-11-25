"""One dialer worker. Any number can run against the same database and campaign."""
from .allocator import CallAllocator
from .config import DialerConfig, SafetyConfig
from .events import EventProcessor
from .ledger import read_snapshot
from .recovery import Housekeeper
from .safety import PermitVerifier, SafetyController


class DialerWorker:
    def __init__(self, worker_id, db, router, pacer, secret: bytes,
                 dcfg: DialerConfig = DialerConfig(), scfg: SafetyConfig = SafetyConfig(),
                 crash_hook=None, new_id=None):
        self.id = worker_id
        self.db = db
        self.router = router
        self.pacer = pacer
        self.dcfg = dcfg
        self.scfg = scfg
        self.events = EventProcessor(db, router, dcfg, crash_hook)
        self.safety = SafetyController(db, scfg, secret, router, new_id)
        self.allocator = CallAllocator(db, router, PermitVerifier(secret), worker_id, dcfg, crash_hook,
                                       new_id)
        self.house = Housekeeper(db, router, self.events, dcfg)
        self.last_decision = None
        self.last_request = None

    def heartbeat(self, now):
        with self.db.tx() as c:
            c.execute("INSERT INTO workers(id, heartbeat) VALUES (?,?) "
                      "ON CONFLICT(id) DO UPDATE SET heartbeat=excluded.heartbeat", (self.id, now))

    def service(self, now):
        """Fast path, run often: apply provider events and time-critical housekeeping."""
        self.events.process_pending(now)
        self.house.bridge_waiting(now)
        self.house.abandon_expired(now)
        self.allocator.process_cancellations(now)

    def pace(self, now):
        """Pacing cycle: snapshot -> pacing engine -> safety controller -> allocator."""
        self.heartbeat(now)
        self.house.run(now)
        self.house.agent_presence(now)
        snap = read_snapshot(self.db.conn(), now, self.scfg.long_window, self.scfg.short_window,
                             self.scfg.abandon_window_s, max_dial_ratio=self.scfg.max_dial_ratio)
        req = self.pacer.request(snap)
        d = self.safety.evaluate(req, self.id, now)
        if d.cancel_unbacked:
            self.allocator.request_cancel_unbacked(d.cancel_unbacked, now)
            self.allocator.process_cancellations(now)
        self.allocator.execute(d.permits, now)
        self.last_request, self.last_decision = req, d
        return d

    def tick(self, now):
        self.service(now)
        return self.pace(now)
