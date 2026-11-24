"""Provider boundary. The dialer only sees this interface and normalised events."""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable, Optional


class ProviderTimeout(Exception):
    """Request outcome unknown: the call may or may not have been placed."""


class ProviderError(Exception):
    """Request definitely rejected."""


@dataclass(frozen=True)
class ProviderEvent:
    provider: str
    event_id: str          # provider's own id; used for exact-duplicate suppression
    call_id: str           # our call id (sent as idempotency key / metadata on dial)
    type: str              # RINGING | ANSWERED | COMPLETED | FAILED
    occurred_at: float
    payload: dict = field(default_factory=dict)


EventSink = Callable[[ProviderEvent], None]


class TelecomProvider(ABC):
    name: str

    def set_sink(self, sink: EventSink) -> None:
        """Where webhooks land. In production this is an HTTP handler that writes the inbox."""
        self.sink = sink

    @abstractmethod
    def dial(self, call_id: str, phone: str, now: float) -> str:
        """Place a call. call_id doubles as the idempotency key. Returns provider call id."""

    @abstractmethod
    def hangup(self, call_id: str, now: float, reason: str = "") -> None: ...

    @abstractmethod
    def bridge(self, call_id: str, agent_id: str, now: float) -> None:
        """Connect an answered call to an agent. Idempotent."""

    @abstractmethod
    def get_status(self, call_id: str, now: float) -> Optional[str]:
        """Provider's view of the call, or None if it has never seen this call_id."""

    def pump(self, now: float) -> int:
        """Mocks deliver due events here. Real providers push via webhook."""
        return 0
