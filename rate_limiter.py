"""
rate_limiter.py
Version: Step 7, v0.7.0 (2026-11-04). Code version 2026-11-step7.
Spec: README v2.0 configuration reference (RATE_LIMIT_PER_MINUTE).

Purpose: a thread-safe limiter that spaces calls at least 60 / per_minute seconds apart.
Recovered from the old reconstruction; the clock and sleep are injectable for tests and
the first call never waits.
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Optional


class RateLimiter:
    def __init__(self, per_minute: int, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.min_interval = 60.0 / max(1, int(per_minute))
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._last: Optional[float] = None

    def acquire(self) -> None:
        with self._lock:
            now = self._clock()
            if self._last is not None:
                wait = self.min_interval - (now - self._last)
                if wait > 0:
                    self._sleep(wait)
                    now = self._clock()
            self._last = now
