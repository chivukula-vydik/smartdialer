"""Safety Controller: the only component that can authorise a dial.

It recomputes everything from the database inside one write transaction, so N workers
asking at the same time are serialised against one global ledger (in-flight calls +
unexpired permits). Its output is a set of single-use, signed, short-lived permits.
The allocator refuses to dial without one."""
import hashlib
import hmac
import json
import math
import uuid
from dataclasses import dataclass, field

from .config import SafetyConfig
from .ledger import read_snapshot
from .models import Mode
from .pacing import PacingRequest

APPROVED, REDUCED, REJECTED, FALLBACK = "APPROVED", "REDUCED", "REJECTED", "FALLBACK_PROGRESSIVE"


@dataclass(frozen=True)
class Permit:
    id: str
    decision_id: str
    mode: Mode
    expires_at: float
    sig: str


@dataclass
class Decision:
    id: str
    at: float
    verdict: str
    requested_mode: Mode
    requested: int
    mode: Mode
    approved: int
    permits: list = field(default_factory=list)
    cancel_unbacked: int = 0
    reasons: list = field(default_factory=list)
    numbers: dict = field(default_factory=dict)


# ---------- statistics ----------

def wilson_upper(k, n, z):
    if n == 0:
        return 1.0
    p = k / n
    d = 1 + z * z / n
    centre = p + z * z / (2 * n)
    spread = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return min(1.0, (centre + spread) / d)


def wilson_lower(k, n, z):
    if n == 0:
        return 0.0
    p = k / n
    d = 1 + z * z / n
    centre = p + z * z / (2 * n)
    spread = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return max(0.0, (centre - spread) / d)


def expected_overflow(m, p, cap):
    """E[(X - cap)+] and E[min(X, cap)] for X ~ Binomial(m, p)."""
    if m <= cap or p <= 0:
        return 0.0, m * p
    if p >= 1:
        return float(m - cap), float(cap)
    if m <= 2000:
        lp, lq = math.log(p), math.log1p(-p)
        lgm = math.lgamma(m + 1)
        over = 0.0
        for k in range(cap + 1, m + 1):
            over += (k - cap) * math.exp(lgm - math.lgamma(k + 1) - math.lgamma(m - k + 1)
                                         + k * lp + (m - k) * lq)
    else:  # normal approximation
        mu, sd = m * p, math.sqrt(m * p * (1 - p))
        d = (cap - mu) / sd
        pdf = math.exp(-d * d / 2) / math.sqrt(2 * math.pi)
        sf = 0.5 * math.erfc(d / math.sqrt(2))
        over = max(0.0, sd * pdf - (cap - mu) * sf)
    return over, m * p - over


def max_safe_inflight(p, cap, eps, hard_max):
    """Largest m <= hard_max with E[overflow] <= eps * E[connects]."""
    if cap <= 0:
        return 0

    def ok(m):
        over, conn = expected_overflow(m, p, cap)
        return over <= eps * max(conn, 1e-9)

    lo, hi = min(cap, hard_max), hard_max   # m <= cap can never overflow
    if ok(hi):
        return hi
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if ok(mid):
            lo = mid
        else:
            hi = mid - 1
    return lo


# ---------- controller ----------

class SafetyController:
    def __init__(self, db, cfg: SafetyConfig, secret: bytes, router=None):
        self.db = db
        self.cfg = cfg
        self._secret = secret
        self.router = router

    def _sign(self, pid, mode, exp, did):
        msg = f"{pid}|{mode}|{exp:.6f}|{did}".encode()
        return hmac.new(self._secret, msg, hashlib.sha256).hexdigest()

    def evaluate(self, req: PacingRequest, worker_id: str, now: float) -> Decision:
        cfg = self.cfg
        did = uuid.uuid4().hex[:12]
        reasons, nums = [], {}
        with self.db.tx() as c:
            s = read_snapshot(c, now, cfg.long_window, cfg.short_window, cfg.abandon_window_s)
            st = c.execute("SELECT * FROM safety_state WHERE id=1").fetchone()
            forced = bool(st["forced_progressive"])

            # -- rolling abandon-rate breaker (shared across workers via the DB) --
            denom = s.connects_window + s.abandons_window
            rate = s.abandon_rate if denom >= cfg.abandon_min_denominator else 0.0
            nums.update(abandon_rate=round(s.abandon_rate, 4), abandon_denominator=denom)
            if not forced and denom >= cfg.abandon_min_denominator and rate >= cfg.trip_abandon_rate:
                forced = True
                c.execute("UPDATE safety_state SET forced_progressive=1, forced_since=?, reason=? "
                          "WHERE id=1", (now, f"abandon rate {rate:.3f} >= {cfg.trip_abandon_rate}"))
                reasons.append(f"TRIP: abandon rate {rate:.2%} >= {cfg.trip_abandon_rate:.2%}")
            elif forced and rate <= cfg.reset_abandon_rate and now - (st["forced_since"] or 0) >= cfg.min_cooldown_s:
                forced = False
                c.execute("UPDATE safety_state SET forced_progressive=0, reason=NULL WHERE id=1")
                reasons.append(f"RESET: abandon rate {rate:.2%} <= {cfg.reset_abandon_rate:.2%}")

            # -- capacity in agents nobody has claimed yet --
            free = max(0, s.available - s.stale_heartbeats - s.answered_waiting
                       - s.open_permits_progressive)
            unbacked = s.unbacked_in_flight + s.open_permits_predictive
            active = (s.unbacked_in_flight + s.backed_in_flight + s.answered_waiting
                      + s.agents.get("CONNECTED", 0))
            # deterministic capacity: treat every unbacked call as a certain answer
            prog_cap = max(0, free - unbacked)
            nums.update(free_agents=free, unbacked_in_flight=unbacked, progressive_capacity=prog_cap,
                        answered_waiting=s.answered_waiting, stale_agents=s.stale_heartbeats)

            verdict, mode, approved, cancel = None, req.mode, 0, 0
            prog_n = 0

            # Safety's own answer-rate estimate: pessimistic upper bound over raw outcomes.
            p_long = wilson_upper(s.answers_long, s.outcomes_long, cfg.ucb_z)
            p_short = wilson_upper(s.answers_short, s.outcomes_short, cfg.ucb_z)
            # Short window only overrides on a real regime shift: its lower bound is above the
            # long window's upper bound. Otherwise its wide interval is just noise.
            shift = (s.outcomes_short >= 10 and
                     wilson_lower(s.answers_short, s.outcomes_short, cfg.ucb_z) > p_long)
            p_floor = (s.recent_answered / s.recent_dialed) if s.recent_dialed >= 20 else 0.0
            p_hi = max(p_short if shift else p_long, p_floor)
            eps = max(0.0, min(cfg.target_abandon_rate, cfg.trip_abandon_rate - rate))

            def stat_bound(cap, e=None):
                e = eps if e is None else e
                hard = max(0, min(math.floor(cfg.max_dial_ratio * cap),
                                  unbacked + max(0, cfg.max_concurrent_calls - active)))
                return (max_safe_inflight(p_hi, cap, e, hard) if e > 0 else min(cap, hard)), hard

            # Existing risk. Only on a shock (agents left, or the answer rate jumped): normal
            # answers also shrink free capacity, and re-tightening on those would turn into
            # ring-and-drop. Looser epsilon than for new calls, for the same reason.
            staffed = s.logged_in
            shock_agents = s.recently_left >= max(2, 0.05 * (staffed + s.recently_left))
            if unbacked and (shock_agents or shift):
                m_now, _ = stat_bound(free, cfg.cancel_abandon_rate)
                if unbacked - m_now >= max(2, 0.1 * m_now):
                    cancel = unbacked - m_now
                    why = (f"{s.recently_left} agents left in 30s" if shock_agents
                           else "answer-rate shift")
                    reasons.append(f"shock ({why}): {unbacked} unbacked > safe {m_now} for "
                                   f"{free} free agents; cancel {cancel}")
            nums.update(p_hi=round(p_hi, 4), p_hi_long=round(p_long, 4),
                        p_hi_short=round(p_short, 4), shift=shift, p_floor_recent=round(p_floor, 4),
                        eps=round(eps, 4))

            if now - req.based_on > cfg.max_request_age_s:
                verdict = REJECTED
                reasons.append(f"request based on snapshot {now - req.based_on:.1f}s old")
            allowance = self.router.allowance(now) if self.router else None
            nums["provider_allowance"] = "unlimited" if allowance is None else allowance
            if verdict is None and allowance == 0:
                verdict = REJECTED
                reasons.append("no provider admitting calls (circuit open)")

            if verdict is None and req.mode == Mode.PREDICTIVE:
                why = None
                if forced:
                    why = "abandon breaker engaged"
                elif s.outcomes_long < cfg.min_outcomes_for_predictive:
                    why = f"cold start: {s.outcomes_long} < {cfg.min_outcomes_for_predictive} outcomes"
                if why:
                    mode, verdict = Mode.PROGRESSIVE, FALLBACK
                    reasons.append("fallback to progressive: " + why)

            if verdict in (None, FALLBACK) and mode == Mode.PROGRESSIVE:
                want = req.calls if req.mode == Mode.PROGRESSIVE else prog_cap
                approved = min(want, prog_cap)

            elif verdict is None and mode == Mode.PREDICTIVE:
                # agent-bound calls first (borrowers previously abandoned), then the statistical budget
                prog_n = min(s.ready_progressive_only, prog_cap)
                cap = free - prog_n
                m_stat, hard = stat_bound(cap)
                room = m_stat - unbacked
                over, conn = expected_overflow(unbacked + max(0, room), p_hi, cap)
                nums.update(cap=cap, max_inflight_stat=m_stat, max_inflight_hard=hard,
                            room=room, exp_overflow=round(over, 3), exp_connects=round(conn, 2),
                            agent_bound_first=prog_n)
                approved = min(req.calls, max(0, room))
                if m_stat == hard and req.calls > approved:
                    reasons.append(f"hard cap: dial ratio {cfg.max_dial_ratio} x {cap} agents")
                elif req.calls > approved:
                    reasons.append(f"overflow bound: p_hi={p_hi:.3f}, eps={eps:.3%} allows "
                                   f"{m_stat} in flight for {cap} agents")

            if allowance is not None and verdict in (None, FALLBACK):
                total = approved + (prog_n if mode == Mode.PREDICTIVE else 0)
                if total > allowance:
                    reasons.append(f"provider half-open: limited to {allowance} probe dials")
                    if mode == Mode.PREDICTIVE:
                        prog_n = min(prog_n, allowance)
                        approved = min(approved, allowance - prog_n)
                    else:
                        approved = min(approved, allowance)

            if verdict is None:
                if approved == req.calls:
                    verdict = APPROVED
                elif approved == 0 and req.calls > 0:
                    verdict = REJECTED
                else:
                    verdict = REDUCED

            permits = []
            exp = now + cfg.permit_ttl_s
            if mode == Mode.PROGRESSIVE:
                plan = [(Mode.PROGRESSIVE, approved)]
            else:
                plan = [(Mode.PROGRESSIVE, prog_n), (Mode.PREDICTIVE, approved)]
            for m, n in plan:
                for _ in range(n):
                    pid = uuid.uuid4().hex
                    sig = self._sign(pid, m.value, exp, did)
                    permits.append(Permit(pid, did, m, exp, sig))
            if permits:
                c.executemany(
                    "INSERT INTO permits(id, decision_id, mode, issued_at, expires_at, sig) "
                    "VALUES (?,?,?,?,?,?)",
                    [(p.id, did, p.mode.value, now, p.expires_at, p.sig) for p in permits])
            if cancel:
                # revoke unused predictive permits before touching live calls
                revoked = c.execute(
                    "UPDATE permits SET revoked=1 WHERE id IN (SELECT id FROM permits WHERE "
                    "mode='PREDICTIVE' AND consumed_by IS NULL AND revoked=0 AND expires_at>? "
                    "ORDER BY issued_at DESC LIMIT ?)", (now, cancel)).rowcount
                cancel -= revoked
                nums["permits_revoked"] = revoked
            d = Decision(did, now, verdict, req.mode, req.calls, mode,
                         len(permits), permits, max(0, cancel), reasons, nums)
            c.execute(
                "INSERT INTO decisions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (did, now, worker_id, req.mode.value, req.calls, d.approved, mode.value, verdict,
                 d.cancel_unbacked, json.dumps(reasons), json.dumps({**nums, "pacing": req.rationale})))
        return d


class PermitVerifier:
    """Given to the allocator. Can check a signature, cannot mint one."""

    def __init__(self, secret: bytes):
        self._secret = secret

    def valid(self, p: Permit, now: float) -> bool:
        if p.expires_at <= now:
            return False
        msg = f"{p.id}|{p.mode.value}|{p.expires_at:.6f}|{p.decision_id}".encode()
        good = hmac.new(self._secret, msg, hashlib.sha256).hexdigest()
        return hmac.compare_digest(good, p.sig)
