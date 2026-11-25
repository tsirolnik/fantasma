"""Per-domain rate limiting."""

import asyncio
import time
from collections import defaultdict


class RateLimiter:
    """Token-bucket rate limiter keyed by domain."""

    def __init__(self, requests_per_second: float = 2.0):
        self.rps = requests_per_second
        self.interval = 1.0 / requests_per_second
        self._last_request: dict[str, float] = defaultdict(float)
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def acquire(self, domain: str) -> None:
        """Wait until we can make a request to the given domain."""
        async with self._locks[domain]:
            now = time.monotonic()
            elapsed = now - self._last_request[domain]
            if elapsed < self.interval:
                await asyncio.sleep(self.interval - elapsed)
            self._last_request[domain] = time.monotonic()
