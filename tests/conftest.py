import os
import sys
from dataclasses import dataclass

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from smartdialer import repo  # noqa: E402
from smartdialer.config import DialerConfig, SafetyConfig  # noqa: E402
from smartdialer.db import Db  # noqa: E402
from smartdialer.health import ProviderRouter  # noqa: E402
from smartdialer.pacing import ProgressivePacer  # noqa: E402
from smartdialer.providers.base import (ProviderError, ProviderEvent,  # noqa: E402
                                        ProviderTimeout, TelecomProvider)
from smartdialer.worker import DialerWorker  # noqa: E402

SECRET = b"test-secret"


class ManualProvider(TelecomProvider):
    """Test double: records dials; the test decides every event and failure."""

    def __init__(self, name="M"):
        self.name = name
        self.calls = {}
        self.dial_mode = "ok"       # ok | timeout_placed | timeout_lost | error
        self.status_mode = "ok"     # ok | timeout
        self.hangups = []
        self.bridges = []
        self._n = 0
        self.sink = None

    def dial(self, call_id, phone, now):
        m = self.dial_mode
        if m == "error":
            raise ProviderError("rejected")
        if m == "timeout_lost":
            raise ProviderTimeout("timeout")
        self.calls[call_id] = "INITIATED"
        if m == "timeout_placed":
            raise ProviderTimeout("timeout")
        return f"pc-{call_id[:6]}"

    def hangup(self, call_id, now, reason=""):
        self.hangups.append((call_id, reason))
        if call_id in self.calls:
            self.calls[call_id] = "COMPLETED"

    def bridge(self, call_id, agent_id, now):
        self.bridges.append((call_id, agent_id))

    def get_status(self, call_id, now):
        if self.status_mode == "timeout":
            raise ProviderTimeout("status timeout")
        return self.calls.get(call_id)

    def event(self, call_id, typ, t, eid=None, **payload):
        self._n += 1
        return ProviderEvent(self.name, eid or f"e{self._n}", call_id, typ, t, payload)


@dataclass
class Stack:
    db: Db
    prov: ManualProvider
    router: ProviderRouter
    worker: DialerWorker

    def new_worker(self, wid, crash_hook=None, pacer=None):
        return DialerWorker(wid, self.db, self.router, pacer or ProgressivePacer(), SECRET,
                            DialerConfig(), SafetyConfig(), crash_hook=crash_hook)

    def agent(self, aid):
        return self.db.one("SELECT * FROM agents WHERE id=?", (aid,))

    def call(self, cid):
        return self.db.one("SELECT * FROM calls WHERE id=?", (cid,))

    def borrower(self, bid):
        return self.db.one("SELECT * FROM borrowers WHERE id=?", (bid,))

    def calls(self):
        return self.db.q("SELECT * FROM calls ORDER BY created_at")


@pytest.fixture
def stack(tmp_path):
    db = Db(str(tmp_path / "t.db"))
    repo.add_agents(db, [f"a{i}" for i in range(3)], 0.0)
    repo.add_borrowers(db, [(f"b{i}", f"+91{i:09d}", 0) for i in range(20)])
    prov = ManualProvider()
    router = ProviderRouter([prov])
    s = Stack(db, prov, router, None)
    s.worker = s.new_worker("w1")
    yield s
    db.close()
