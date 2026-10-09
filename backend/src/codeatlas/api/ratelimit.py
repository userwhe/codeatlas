"""Per-address rate limit for the endpoints without a session (FR-006, research R4).

The API runs in one process, so a sliding window kept in memory is exact. A shared counter is
needed only once the API runs in several processes; the interface would stay the same.
"""

import math
import threading
import time
from collections import deque
from collections.abc import Callable

SWEEP_INTERVAL_SECONDS = 60.0


class SlidingWindowLimiter:
    """Allows `limit` calls per address in any `window_seconds`; a `limit` of 0 allows all.

    Refused calls are not counted, so a client that keeps retrying is let in again once its
    oldest allowed call leaves the window.
    """

    def __init__(
        self,
        limit: int,
        window_seconds: float = 60,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._limit = limit
        self._window = window_seconds
        self._clock = clock
        self._calls: dict[str, deque[float]] = {}
        self._lock = threading.Lock()
        self._last_sweep = clock()

    def check(self, address: str) -> float | None:
        """Count a call from `address`. Returns None when it is allowed, or else the seconds,
        rounded up, until the address's oldest call leaves the window.
        """
        if self._limit <= 0:
            return None
        with self._lock:
            now = self._clock()
            if now - self._last_sweep >= SWEEP_INTERVAL_SECONDS:
                self._sweep(now)
            calls = self._calls.setdefault(address, deque())
            while calls and calls[0] <= now - self._window:
                calls.popleft()
            if len(calls) >= self._limit:
                return float(math.ceil(calls[0] + self._window - now))
            calls.append(now)
            return None

    def sweep(self) -> None:
        """Forget the addresses whose newest call left the window."""
        with self._lock:
            self._sweep(self._clock())

    def __len__(self) -> int:
        """The number of addresses tracked."""
        with self._lock:
            return len(self._calls)

    def _sweep(self, now: float) -> None:
        self._last_sweep = now
        idle = [
            address
            for address, calls in self._calls.items()
            if not calls or calls[-1] <= now - self._window
        ]
        for address in idle:
            del self._calls[address]
