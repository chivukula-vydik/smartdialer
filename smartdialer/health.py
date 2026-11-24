"""Per-provider circuit breaker and router."""
from collections import deque

CLOSED, OPEN, HALF_OPEN = "CLOSED", "OPEN", "HALF_OPEN"


class CircuitBreaker:
    def __init__(self, window=20, min_samples=5, fail_ratio=0.5, consecutive_timeouts=3,
                 open_s=10.0, half_open_probes=2):
        self.window = deque(maxlen=window)
        self.min_samples = min_samples
        self.fail_ratio = fail_ratio
        self.consecutive_timeouts = consecutive_timeouts
        self.open_s = open_s
        self.half_open_probes = half_open_probes
        self._state = CLOSED
        self._opened_at = 0.0
        self._probes_left = 0
        self._consec_to = 0
        self.trips = 0

    def state(self, now):
        if self._state == OPEN and now - self._opened_at >= self.open_s:
            self._state = HALF_OPEN
            self._probes_left = self.half_open_probes
        return self._state

    def allowance(self, now):
        """How many new dials this breaker lets through right now (None = unlimited)."""
        s = self.state(now)
        if s == OPEN:
            return 0
        if s == HALF_OPEN:
            return self._probes_left
        return None

    def take(self, now):
        s = self.state(now)
        if s == OPEN:
            return False
        if s == HALF_OPEN:
            if self._probes_left <= 0:
                return False
            self._probes_left -= 1
        return True

    def success(self, now):
        self.window.append(True)
        self._consec_to = 0
        if self.state(now) == HALF_OPEN:
            self._state = CLOSED
            self.window.clear()

    def failure(self, now, timeout=False):
        self.window.append(False)
        self._consec_to = self._consec_to + 1 if timeout else 0
        s = self.state(now)
        fails = self.window.count(False)
        if s == HALF_OPEN or self._consec_to >= self.consecutive_timeouts or (
                len(self.window) >= self.min_samples and fails / len(self.window) >= self.fail_ratio):
            if s != OPEN:
                self.trips += 1
            self._state = OPEN
            self._opened_at = now
            self._consec_to = 0
            self.window.clear()


class ProviderRouter:
    """Ordered list of providers; first one whose breaker admits a dial wins."""

    def __init__(self, providers, breaker_factory=CircuitBreaker):
        self.providers = {p.name: p for p in providers}
        self.order = [p.name for p in providers]
        self.breakers = {p.name: breaker_factory() for p in providers}

    def get(self, name):
        return self.providers[name]

    def pick(self, now):
        for n in self.order:
            if self.breakers[n].take(now):
                return self.providers[n]
        return None

    def allowance(self, now):
        """Total dials the providers will accept now (None = unlimited)."""
        total = 0
        for n in self.order:
            a = self.breakers[n].allowance(now)
            if a is None:
                return None
            total += a
        return total

    def states(self, now):
        return {n: self.breakers[n].state(now) for n in self.order}
