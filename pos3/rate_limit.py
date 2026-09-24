"""One byte rate that any number of threads share."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable


class ByteRateLimiter:
    """Paces bytes from all callers to one total rate.

    Each call reserves the next slot on one shared timeline and sleeps until that slot ends. So the
    bytes released since the first call never exceed ``bytes_per_second`` times the elapsed time. An
    idle limiter builds no credit, so a burst after a pause gets no extra rate.
    """

    def __init__(
        self,
        bytes_per_second: float,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if bytes_per_second <= 0:
            raise ValueError(f"bytes_per_second must be positive, got {bytes_per_second}")
        self.bytes_per_second = bytes_per_second
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._next_free = float("-inf")

    def consume(self, n_bytes: int) -> None:
        """Block until ``n_bytes`` fit under the rate."""
        # s3transfer reports the rewind before a retry as a negative count; those bytes were paced already.
        if n_bytes <= 0:
            return
        with self._lock:
            now = self._clock()
            self._next_free = max(now, self._next_free) + n_bytes / self.bytes_per_second
            wait = self._next_free - now
        self._sleep(wait)
