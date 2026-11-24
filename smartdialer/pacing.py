"""Pacing engines. They only see a frozen Snapshot and only return a PacingRequest.
They hold no reference to the provider, the allocator or the safety config."""
import math
from dataclasses import dataclass, field

from .ledger import Snapshot
from .models import Mode


@dataclass(frozen=True)
class PacingRequest:
    mode: Mode
    calls: int
    based_on: float               # snapshot timestamp
    rationale: dict = field(default_factory=dict)


class ProgressivePacer:
    mode = Mode.PROGRESSIVE

    def request(self, s: Snapshot) -> PacingRequest:
        free = s.available - s.answered_waiting - s.open_permits_progressive
        return PacingRequest(Mode.PROGRESSIVE, max(0, free), s.taken_at,
                             {"free_agents": s.available, "open_permits": s.open_permits_progressive})


class PredictivePacer:
    """Expected-value pacing.

    capacity  = free agents + agents expected to free up before new calls get answered
    demand    = answers expected from calls already ringing
    new_calls = (capacity - demand) / p

    p comes from a Beta prior blended with the recent window, so a cold campaign starts
    near the prior instead of at 0/0. Aggressiveness > 1 deliberately over-asks; the Safety
    Controller is the one that says no."""
    mode = Mode.PREDICTIVE

    def __init__(self, prior_p=0.3, prior_weight=10, aggressiveness=1.0, wrap_s=10.0,
                 default_ring_s=12.0, default_talk_s=120.0):
        self.prior_p = prior_p
        self.prior_w = prior_weight
        self.aggr = aggressiveness
        self.wrap_s = wrap_s
        self.default_ring_s = default_ring_s
        self.default_talk_s = default_talk_s

    def answer_rate(self, s: Snapshot) -> float:
        # Short window weighted 2x so a regime change shows up within ~40 outcomes.
        a = self.prior_p * self.prior_w + s.answers_long + s.answers_short
        n = self.prior_w + s.outcomes_long + s.outcomes_short
        p = a / n
        if s.recent_dialed >= 20:   # answers already seen in the unresolved cohort
            p = max(p, s.recent_answered / s.recent_dialed)
        return min(0.95, max(0.02, p))

    def request(self, s: Snapshot) -> PacingRequest:
        p = self.answer_rate(s)
        ring = s.avg_ring_s or self.default_ring_s
        talk = s.avg_talk_s or self.default_talk_s
        horizon = ring + 1.0                     # time until newly dialed calls start answering
        connected = s.agents.get("CONNECTED", 0)
        wrap = s.agents.get("WRAP_UP", 0)
        soon_free = connected * min(1.0, horizon / talk) + wrap * min(1.0, horizon / self.wrap_s)
        capacity = s.available + soon_free - s.answered_waiting
        in_flight = s.unbacked_in_flight + s.open_permits_predictive
        expected_answers = in_flight * p
        deficit = capacity - expected_answers
        n = max(0, math.floor(self.aggr * deficit / p)) if deficit > 0 else 0
        return PacingRequest(Mode.PREDICTIVE, n, s.taken_at, {
            "p_hat": round(p, 4), "free_agents": s.available,
            "soon_free": round(soon_free, 2), "capacity": round(capacity, 2),
            "in_flight_unbacked": in_flight, "expected_answers_in_flight": round(expected_answers, 2),
            "deficit": round(deficit, 2), "ring_s": round(ring, 1), "talk_s": round(talk, 1),
            "formula": "floor(aggr * (capacity - in_flight*p) / p)"})
