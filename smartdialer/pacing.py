"""Pacing engines. They only see a frozen Snapshot and only return a PacingRequest.
They hold no reference to the provider, the allocator or the safety config."""
import math
from dataclasses import dataclass, field

from .ledger import Snapshot
from .models import Mode
from .stats import max_safe_inflight


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
    """Pacing with the same overflow model the Safety Controller uses, at the expected answer rate.

    C = agents free now (not already promised to a call)
    p = expected (mean) answer rate
    m = largest number of unbacked calls in flight with
        E[(X - C)+] <= target_abandon * E[min(X, C)],  X ~ Binomial(m, p)
    request = m - calls already in flight

    The pacer uses the same agents and the same 4x dial-ratio ceiling as safety, and differs in
    exactly two deliberate ways: it uses the mean answer rate instead of an upper confidence bound,
    and it aims at the regulatory limit (3%) instead of safety's 1.5% margin. So it always asks for at least what safety allows; the difference is
    the cost of the safety margin. p comes from a Beta prior blended with the recent window, so a
    cold campaign starts near the prior instead of at 0/0. Aggressiveness > 1 deliberately
    over-asks."""
    mode = Mode.PREDICTIVE

    def __init__(self, prior_p=0.3, prior_weight=10, aggressiveness=1.0, wrap_s=10.0,
                 default_ring_s=12.0, default_talk_s=120.0, target_abandon=0.03, max_ratio=4.0):
        self.prior_p = prior_p
        self.prior_w = prior_weight
        self.aggr = aggressiveness
        self.wrap_s = wrap_s
        self.default_ring_s = default_ring_s
        self.default_talk_s = default_talk_s
        self.target_abandon = target_abandon
        self.max_ratio = max_ratio

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
        capacity = max(0, s.available - s.stale_heartbeats - s.answered_waiting
                       - s.open_permits_progressive)
        in_flight = s.unbacked_in_flight + s.open_permits_predictive
        hard = max(in_flight, math.floor(self.max_ratio * capacity))
        target = max_safe_inflight(p, capacity, self.target_abandon, hard)
        room = target - in_flight
        n = max(0, math.floor(self.aggr * room))
        return PacingRequest(Mode.PREDICTIVE, n, s.taken_at, {
            "p_hat": round(p, 4), "free_agents": s.available,
            "capacity": capacity,
            "in_flight_unbacked": in_flight, "expected_answers_in_flight": round(in_flight * p, 2),
            "target_in_flight": target, "target_abandon": self.target_abandon,
            "ring_s": round(ring, 1), "talk_s": round(talk, 1),
            "formula": "largest m with E[(X-C)+] <= target * E[min(X,C)], X~Bin(m,p_hat); "
                       "request = aggr * (m - in_flight)"})
