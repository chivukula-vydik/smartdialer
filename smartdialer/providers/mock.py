"""Mock telecom providers driven by a (virtual or real) clock.

The provider owns the "network": it decides who answers, how long they ring and talk, and
delivers webhooks with configurable delay, duplication, reordering and outages."""
import heapq
import itertools
import random
from dataclasses import dataclass
from typing import Callable, Optional

from .base import ProviderError, ProviderEvent, ProviderTimeout, TelecomProvider


class Population:
    """Borrower behaviour. answer_rate and talk_time may be functions of time (scenario D)."""

    def __init__(self, answer_rate, talk_time_s, ring_to_answer=(3.0, 12.0),
                 no_answer_after_s=25.0, busy_share=0.15, patience=(3.0, 8.0)):
        self._ar = answer_rate if callable(answer_rate) else (lambda t, v=answer_rate: v)
        self._tt = talk_time_s if callable(talk_time_s) else (lambda t, v=talk_time_s: v)
        self.ring_to_answer = ring_to_answer
        self.no_answer_after_s = no_answer_after_s
        self.busy_share = busy_share
        self.patience = patience

    def answer_rate(self, t):
        return self._ar(t)

    def talk_time(self, t, rng):
        mean = self._tt(t)
        return max(10.0, rng.gammavariate(4.0, mean / 4.0))


@dataclass
class MockConfig:
    name: str
    setup_s: tuple = (0.5, 1.5)          # dial -> RINGING
    delivery_s: tuple = (0.02, 0.15)     # webhook latency
    late_share: float = 0.0              # share of events with extra delay (causes reordering)
    late_extra_s: tuple = (0.0, 0.0)
    reject_rate: float = 0.005
    timeout_rate: float = 0.002
    placed_on_timeout: float = 0.5       # a timed-out dial may still have gone out
    dup_rate: float = 0.0                # same event_id delivered twice
    dup_new_id_rate: float = 0.0         # same event re-sent with a new event_id


PROVIDER_A = MockConfig(name="A")
PROVIDER_B = MockConfig(name="B", setup_s=(2.0, 5.0), delivery_s=(0.2, 0.9), late_share=0.15,
                        late_extra_s=(0.5, 2.5), reject_rate=0.03, timeout_rate=0.05,
                        dup_rate=0.15, dup_new_id_rate=0.03)


class MockProvider(TelecomProvider):
    def __init__(self, cfg: MockConfig, population: Population, seed: int = 0):
        self.name = cfg.name
        self.cfg = cfg
        self.pop = population
        self.rng = random.Random(seed)
        self.sink: Optional[Callable] = None
        self.calls = {}               # call_id -> dict(status, bridged, ...)
        self._net = []                # (t, seq, action, call_id)
        self._outbox = []             # (deliver_at, seq, ProviderEvent)
        self._seq = itertools.count()
        self._eid = itertools.count()
        self.outage = False
        self.stats = dict(dials=0, timeouts=0, rejects=0, events=0, dups=0)

    # ---- controls ----
    def set_outage(self, on: bool):
        self.outage = on

    # ---- API ----
    def dial(self, call_id, phone, now):
        if call_id in self.calls:                       # idempotent on our call id
            return self.calls[call_id]["pcid"]
        self.stats["dials"] += 1
        r = self.rng.random()
        if self.outage or r < self.cfg.timeout_rate:
            self.stats["timeouts"] += 1
            if self.rng.random() < self.cfg.placed_on_timeout:
                self._place(call_id, now)
            raise ProviderTimeout(f"{self.name}: dial timeout")
        if r < self.cfg.timeout_rate + self.cfg.reject_rate:
            self.stats["rejects"] += 1
            raise ProviderError(f"{self.name}: dial rejected")
        return self._place(call_id, now)

    def hangup(self, call_id, now, reason="hangup"):
        if self.outage:
            raise ProviderTimeout(f"{self.name}: hangup timeout")
        c = self.calls.get(call_id)
        if c is None or c["status"] == "ended":
            return
        answered = c["status"] in ("answered", "bridged")
        c["status"] = "ended"
        self._emit(call_id, "COMPLETED", now, answered=answered, reason=reason)

    def bridge(self, call_id, agent_id, now):
        if self.outage:
            raise ProviderTimeout(f"{self.name}: bridge timeout")
        c = self.calls.get(call_id)
        if c is None or c["status"] in ("ended", "ringing", "pending"):
            raise ProviderError(f"{self.name}: cannot bridge call in state "
                                f"{None if c is None else c['status']}")
        if c["status"] == "bridged":
            return
        c["status"] = "bridged"
        c["agent"] = agent_id
        self._at(now + self.pop.talk_time(now, self.rng), "talk_end", call_id)

    def get_status(self, call_id, now):
        if self.outage:
            raise ProviderTimeout(f"{self.name}: status timeout")
        c = self.calls.get(call_id)
        if c is None:
            return None
        return {"pending": "INITIATED", "ringing": "RINGING", "answered": "ANSWERED",
                "bridged": "CONNECTED", "ended": c.get("final", "COMPLETED")}[c["status"]]

    # ---- network simulation ----
    def _place(self, call_id, now):
        pcid = f"{self.name}-pc-{next(self._seq)}"
        self.calls[call_id] = {"status": "pending", "pcid": pcid, "bridged": False}
        t_ring = now + self.rng.uniform(*self.cfg.setup_s)
        self._at(t_ring, "ring", call_id)
        p = self.pop.answer_rate(now)
        u = self.rng.random()
        if u < p:
            self._at(t_ring + self.rng.uniform(*self.pop.ring_to_answer), "answer", call_id)
        elif u < p + (1 - p) * self.pop.busy_share:
            self._at(t_ring + 1.0, "busy", call_id)
        else:
            self._at(t_ring + self.pop.no_answer_after_s, "noanswer", call_id)
        return pcid

    def _at(self, t, action, call_id):
        heapq.heappush(self._net, (t, next(self._seq), action, call_id))

    def _emit(self, call_id, typ, t, **payload):
        ev = ProviderEvent(self.name, f"{self.name}-ev-{next(self._eid)}", call_id, typ, t, payload)
        self._queue(ev, t)
        if self.rng.random() < self.cfg.dup_rate:
            self.stats["dups"] += 1
            self._queue(ev, t + self.rng.uniform(0.0, 2.0))
        if self.rng.random() < self.cfg.dup_new_id_rate:
            self.stats["dups"] += 1
            ev2 = ProviderEvent(self.name, f"{self.name}-ev-{next(self._eid)}", call_id, typ, t, payload)
            self._queue(ev2, t + self.rng.uniform(0.0, 2.0))

    def _queue(self, ev, t):
        d = self.rng.uniform(*self.cfg.delivery_s)
        if self.rng.random() < self.cfg.late_share:
            d += self.rng.uniform(*self.cfg.late_extra_s)
        heapq.heappush(self._outbox, (t + d, next(self._seq), ev))

    def _step_network(self, t, action, call_id):
        c = self.calls[call_id]
        st = c["status"]
        if action == "ring" and st == "pending":
            c["status"] = "ringing"
            self._emit(call_id, "RINGING", t)
        elif action == "answer" and st in ("pending", "ringing"):
            c["status"] = "answered"
            self._emit(call_id, "ANSWERED", t)
            self._at(t + self.rng.uniform(*self.pop.patience), "patience", call_id)
        elif action == "busy" and st in ("pending", "ringing"):
            c["status"], c["final"] = "ended", "FAILED"
            self._emit(call_id, "FAILED", t, reason="busy")
        elif action == "noanswer" and st in ("pending", "ringing"):
            c["status"], c["final"] = "ended", "FAILED"
            self._emit(call_id, "COMPLETED", t, answered=False, reason="no_answer")
        elif action == "patience" and st == "answered":
            c["status"] = "ended"
            self._emit(call_id, "COMPLETED", t, answered=True, reason="caller_hangup")
        elif action == "talk_end" and st == "bridged":
            c["status"] = "ended"
            self._emit(call_id, "COMPLETED", t, answered=True, reason="normal")

    def pump(self, now):
        while self._net and self._net[0][0] <= now:
            t, _, action, call_id = heapq.heappop(self._net)
            self._step_network(t, action, call_id)
        n = 0
        if self.outage:
            return 0          # webhooks are not getting through
        while self._outbox and self._outbox[0][0] <= now:
            _, _, ev = heapq.heappop(self._outbox)
            self.stats["events"] += 1
            n += 1
            if self.sink:
                self.sink(ev)
        return n
