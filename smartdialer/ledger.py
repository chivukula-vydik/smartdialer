"""Read model: counts and raw statistics derived from the source-of-truth tables."""
from dataclasses import dataclass, field

from .config import SafetyConfig

ACTIVE_STATES = "('QUEUED','RESERVED','INITIATED','RINGING','ANSWERED','CONNECTED')"
_DEFAULT_RATIO = SafetyConfig().max_dial_ratio   # single source: SafetyConfig


@dataclass(frozen=True)
class Snapshot:
    taken_at: float
    agents: dict = field(default_factory=dict)   # state -> count
    unbacked_in_flight: int = 0     # unanswered calls with no agent attached (predictive risk)
    backed_in_flight: int = 0       # unanswered calls with a reserved agent (progressive)
    answered_waiting: int = 0       # answered, no agent yet (abandon clock running)
    open_permits_progressive: int = 0
    open_permits_predictive: int = 0
    outcomes_long: int = 0
    answers_long: int = 0
    outcomes_short: int = 0
    answers_short: int = 0
    avg_ring_s: float = 0.0
    avg_talk_s: float = 0.0
    connects_window: int = 0
    abandons_window: int = 0
    stale_heartbeats: int = 0
    ready_progressive_only: int = 0   # borrowers who were abandoned before; agent-bound only
    recent_dialed: int = 0            # dialed within the resolve lag (outcomes still arriving)
    recent_answered: int = 0
    recently_left: int = 0            # agents that went OFFLINE/PAUSED in the last 30s
    max_dial_ratio: float = _DEFAULT_RATIO  # safety's published ceiling; the pacer plans within it

    @property
    def available(self):
        return self.agents.get("AVAILABLE", 0)

    @property
    def logged_in(self):
        return sum(v for k, v in self.agents.items() if k != "OFFLINE")

    @property
    def abandon_rate(self):
        d = self.connects_window + self.abandons_window
        return self.abandons_window / d if d else 0.0


def read_snapshot(c, now, long_window=400, short_window=40, abandon_window_s=900.0,
                  presence_ttl_s=15.0, stats_n=200, resolve_lag_s=30.0,
                  max_dial_ratio=_DEFAULT_RATIO):
    # Counts come from trigger-maintained counter rows (see db.py), not table scans.
    agents = {r[0]: r[1] for r in c.execute("SELECT state, n FROM agent_counts WHERE n > 0")}
    unbacked = backed = waiting = 0
    for st, no_agent, n in c.execute(
            f"SELECT state, unbacked, n FROM call_counts WHERE state IN {ACTIVE_STATES} AND n > 0"):
        if st in ("QUEUED", "RESERVED", "INITIATED", "RINGING"):
            if no_agent:
                unbacked += n
            else:
                backed += n
        elif st == "ANSWERED" and no_agent:
            waiting += n
    perm = {r[0]: r[1] for r in c.execute(
        "SELECT mode, COUNT(*) FROM permits WHERE consumed_by IS NULL AND revoked=0 "
        "AND expires_at>? GROUP BY mode", (now,))}
    # Cohort by dial time, only calls old enough that every outcome is known. Ordering by
    # outcome time instead is biased: answers resolve in ~8s, no-answers in ~25s, so the
    # newest outcomes over-represent answers.
    outs = [r[0] for r in c.execute(
        "SELECT net_outcome FROM calls WHERE dialed_at<? AND net_outcome IS NOT NULL "
        "ORDER BY dialed_at DESC LIMIT ?", (now - resolve_lag_s, long_window))]
    short = outs[:short_window]
    # Recent unresolved cohort: answers seen so far are a hard lower bound on its answer rate.
    rec = c.execute(
        "SELECT COUNT(*), COALESCE(SUM(net_outcome='ANSWER'),0) FROM calls "
        "WHERE dialed_at>=? AND provider_call_id IS NOT NULL", (now - resolve_lag_s,)).fetchone()
    ring = c.execute(
        "SELECT AVG(answered_at - dialed_at) FROM (SELECT answered_at, dialed_at FROM calls "
        "WHERE net_outcome='ANSWER' AND dialed_at IS NOT NULL ORDER BY dialed_at DESC LIMIT ?)",
        (stats_n,)).fetchone()[0]
    talk = c.execute(
        "SELECT AVG(ended_at - connected_at) FROM (SELECT ended_at, connected_at FROM calls "
        "WHERE state='COMPLETED' AND connected_at IS NOT NULL ORDER BY ended_at DESC LIMIT ?)",
        (stats_n,)).fetchone()[0]
    since = now - abandon_window_s
    connects = c.execute("SELECT COUNT(*) FROM calls WHERE connected_at>=?", (since,)).fetchone()[0]
    abandons = c.execute("SELECT COUNT(*) FROM calls WHERE state='ABANDONED' AND ended_at>=?",
                         (since,)).fetchone()[0]
    prog_only = c.execute(
        "SELECT COUNT(*) FROM borrowers WHERE status='PENDING' AND next_attempt_at<=? "
        "AND progressive_only=1", (now,)).fetchone()[0]
    left = c.execute("SELECT COUNT(*) FROM agents WHERE state IN ('OFFLINE','PAUSED') "
                     "AND state_since>=?", (now - 30.0,)).fetchone()[0]
    stale = c.execute("SELECT COUNT(*) FROM agents WHERE state='AVAILABLE' AND last_heartbeat<?",
                      (now - presence_ttl_s,)).fetchone()[0]
    return Snapshot(
        taken_at=now, max_dial_ratio=max_dial_ratio, agents=agents, unbacked_in_flight=unbacked, backed_in_flight=backed,
        answered_waiting=waiting,
        open_permits_progressive=perm.get("PROGRESSIVE", 0),
        open_permits_predictive=perm.get("PREDICTIVE", 0),
        outcomes_long=len(outs), answers_long=sum(1 for o in outs if o == "ANSWER"),
        outcomes_short=len(short), answers_short=sum(1 for o in short if o == "ANSWER"),
        avg_ring_s=ring or 0.0, avg_talk_s=talk or 0.0,
        connects_window=connects, abandons_window=abandons, stale_heartbeats=stale,
        ready_progressive_only=prog_only, recent_dialed=rec[0], recent_answered=rec[1],
        recently_left=left)
