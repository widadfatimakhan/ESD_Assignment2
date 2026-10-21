"""A small in-memory rate limiter (sliding window), used on GET /availability.

Rule: each client may make at most LIMIT calls in any WINDOW_SECONDS period.
The limiter remembers the time of each client's recent calls. When a client
already has LIMIT calls inside the window, the next call is refused, and
Retry-After says how many seconds until its oldest call leaves the window.

"Single instance" on purpose: the counts live in this process's memory, so
they reset when the container restarts and would not be shared if we ran
several copies of the API. (Several copies would need a shared store such
as Redis.)
"""
import math
import threading
import time
from collections import defaultdict, deque


class RateLimitExceeded(Exception):
    """Raised when a client has used up its budget. main.py turns this into a 429."""

    def __init__(self, client: str, limit: int, window_seconds: int, retry_after: int):
        super().__init__(f"rate limit exceeded for {client}")
        self.client = client
        self.limit = limit
        self.window_seconds = window_seconds
        self.retry_after = retry_after


class SlidingWindowRateLimiter:
    def __init__(self, limit: int, window_seconds: int):
        self.limit = limit
        self.window_seconds = window_seconds
        self._calls: dict[str, deque[float]] = defaultdict(deque)   # client -> times of recent calls
        self._lock = threading.Lock()   # requests are handled on several threads at once

    def check(self, client: str) -> int:
        """Record a call by `client`. Returns how many calls it has left in the window.

        Raises RateLimitExceeded (and does NOT record the call) if the client is over the limit.
        """
        now = time.monotonic()
        with self._lock:
            calls = self._calls[client]
            # Forget calls that are older than the window.
            while calls and calls[0] <= now - self.window_seconds:
                calls.popleft()
            if len(calls) >= self.limit:
                # The oldest call leaves the window at calls[0] + window.
                retry_after = max(1, math.ceil(calls[0] + self.window_seconds - now))
                raise RateLimitExceeded(client, self.limit, self.window_seconds, retry_after)
            calls.append(now)
            return self.limit - len(calls)

    def reset(self) -> None:
        """Forget every client's history (used by tests)."""
        with self._lock:
            self._calls.clear()