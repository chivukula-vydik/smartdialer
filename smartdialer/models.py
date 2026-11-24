"""Agent and call state machines. Pure data, no I/O."""
from enum import Enum


class AgentState(str, Enum):
    OFFLINE = "OFFLINE"
    AVAILABLE = "AVAILABLE"
    RESERVED = "RESERVED"      # bound to a call that is being set up (progressive)
    DIALING = "DIALING"        # provider accepted the call, waiting for answer
    CONNECTED = "CONNECTED"
    WRAP_UP = "WRAP_UP"
    PAUSED = "PAUSED"


A = AgentState
AGENT_TRANSITIONS = {
    A.OFFLINE: {A.AVAILABLE},
    A.AVAILABLE: {A.RESERVED, A.CONNECTED, A.PAUSED, A.OFFLINE},
    A.RESERVED: {A.DIALING, A.CONNECTED, A.AVAILABLE, A.OFFLINE},
    A.DIALING: {A.CONNECTED, A.AVAILABLE, A.OFFLINE},
    A.CONNECTED: {A.WRAP_UP},
    A.WRAP_UP: {A.AVAILABLE, A.PAUSED, A.OFFLINE},
    A.PAUSED: {A.AVAILABLE, A.OFFLINE},
}


class CallState(str, Enum):
    QUEUED = "QUEUED"          # row exists, no agent (predictive) and not yet sent to provider
    RESERVED = "RESERVED"      # agent reserved (progressive), not yet sent to provider
    INITIATED = "INITIATED"    # provider accepted the dial request
    RINGING = "RINGING"
    ANSWERED = "ANSWERED"      # human answered, not yet bridged to an agent
    CONNECTED = "CONNECTED"    # bridged to an agent
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"          # no answer, busy, provider error, lost
    CANCELLED = "CANCELLED"    # we hung up before answer
    ABANDONED = "ABANDONED"    # answered but no agent within the grace period


C = CallState
TERMINAL = frozenset({C.COMPLETED, C.FAILED, C.CANCELLED, C.ABANDONED})
PRE_DIAL = frozenset({C.QUEUED, C.RESERVED})
UNANSWERED_ACTIVE = frozenset({C.QUEUED, C.RESERVED, C.INITIATED, C.RINGING})
ACTIVE = frozenset(set(C) - TERMINAL)

# Monotonic rank. An event can never move a call to a lower rank.
RANK = {C.QUEUED: 0, C.RESERVED: 1, C.INITIATED: 2, C.RINGING: 3,
        C.ANSWERED: 4, C.CONNECTED: 5,
        C.COMPLETED: 9, C.FAILED: 9, C.CANCELLED: 9, C.ABANDONED: 9}

CALL_TRANSITIONS = {
    # Provider events are evidence the dial happened, so pre-dial states may jump forward.
    C.QUEUED: {C.INITIATED, C.RINGING, C.ANSWERED, C.COMPLETED, C.FAILED, C.CANCELLED},
    C.RESERVED: {C.INITIATED, C.RINGING, C.ANSWERED, C.COMPLETED, C.FAILED, C.CANCELLED},
    C.INITIATED: {C.RINGING, C.ANSWERED, C.COMPLETED, C.FAILED, C.CANCELLED},
    C.RINGING: {C.ANSWERED, C.COMPLETED, C.FAILED, C.CANCELLED},
    C.ANSWERED: {C.CONNECTED, C.COMPLETED, C.ABANDONED, C.FAILED},
    C.CONNECTED: {C.COMPLETED, C.FAILED},
}
for _t in TERMINAL:
    CALL_TRANSITIONS[_t] = set()


class Mode(str, Enum):
    PROGRESSIVE = "PROGRESSIVE"
    PREDICTIVE = "PREDICTIVE"


class BorrowerStatus(str, Enum):
    PENDING = "PENDING"
    CLAIMED = "CLAIMED"
    DONE = "DONE"
    EXHAUSTED = "EXHAUSTED"


# Provider event types (normalised by each provider adapter)
EV_RINGING = "RINGING"
EV_ANSWERED = "ANSWERED"
EV_COMPLETED = "COMPLETED"
EV_FAILED = "FAILED"

_EVENT_TARGET = {EV_RINGING: C.RINGING, EV_ANSWERED: C.ANSWERED, EV_FAILED: C.FAILED}


def event_target(current: CallState, ev_type: str, payload: dict) -> CallState:
    """Map a provider event to the state it implies, given what we already know."""
    if ev_type == EV_COMPLETED:
        answered = payload.get("answered") or current in (C.ANSWERED, C.CONNECTED)
        return C.COMPLETED if answered else C.FAILED
    if ev_type not in _EVENT_TARGET:
        raise ValueError(f"unknown event type {ev_type}")
    return _EVENT_TARGET[ev_type]


def resolve_event(current: CallState, ev_type: str, payload: dict):
    """Return (target_state or None, outcome). Pure; used by the event processor.

    outcome is one of: applied, duplicate, stale, after_terminal, invalid.
    """
    if current in TERMINAL:
        return None, "after_terminal"
    target = event_target(current, ev_type, payload)
    if target == current:
        return None, "duplicate"
    if RANK[target] < RANK[current]:
        return None, "stale"
    if target in CALL_TRANSITIONS[current]:
        return target, "applied"
    return None, "invalid"


def can_agent(src: AgentState, dst: AgentState) -> bool:
    return dst in AGENT_TRANSITIONS[src]
