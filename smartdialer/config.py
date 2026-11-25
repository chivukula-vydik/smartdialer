from dataclasses import dataclass


@dataclass(frozen=True)
class DialerConfig:
    abandon_grace_s: float = 2.0        # answered call must reach an agent within this
    dial_lease_s: float = 10.0          # how long a worker owns a call while dialing
    reconcile_after_s: float = 3.0      # uncertain dial: ask provider after this long
    max_uncertain_s: float = 30.0       # give up on an uncertain dial after this long
    stale_call_s: float = 90.0          # active call with no event for this long gets reconciled
    agent_presence_ttl_s: float = 15.0  # agent heartbeat timeout
    worker_ttl_s: float = 10.0
    retry_noanswer_s: float = 300.0
    retry_abandon_s: float = 600.0
    retry_requeue_s: float = 1.0
    provider_backoff_base_s: float = 5.0
    provider_backoff_cap_s: float = 300.0
    max_provider_failures: int = 8
    permit_retention_s: float = 600.0    # delete used/expired permits after this
    event_retention_s: float = 3600.0    # delete processed inbox events after this
    prune_every_s: float = 60.0


@dataclass(frozen=True)
class SafetyConfig:
    """Owned by operators/compliance. The pacing engine never sees or mutates this."""
    max_abandon_rate: float = 0.03       # regulatory ceiling
    target_abandon_rate: float = 0.015   # per-decision expected overflow budget (epsilon)
    cancel_abandon_rate: float = 0.06    # existing calls riskier than this get cancelled
    trip_abandon_rate: float = 0.025     # rolling rate that forces progressive
    reset_abandon_rate: float = 0.010    # rolling rate that releases the fallback
    abandon_window_s: float = 900.0
    min_cooldown_s: float = 60.0
    min_outcomes_for_predictive: int = 40
    long_window: int = 400
    short_window: int = 40
    ucb_z: float = 1.645                 # one-sided 95% Wilson upper bound
    max_dial_ratio: float = 4.0          # unbacked in-flight <= ratio * free agents
    max_concurrent_calls: int = 100_000  # provider/trunk concurrency ceiling
    max_request_age_s: float = 2.0
    permit_ttl_s: float = 3.0
    abandon_min_denominator: int = 30
    calling_window: tuple = (8 * 3600, 19 * 3600)  # local seconds of day: no dials outside 08:00-19:00
    clock_offset_s: float = 19800.0     # added to `now` to get local time (epoch clock in IST = +5:30)
