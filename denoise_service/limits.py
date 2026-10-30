"""Per-client rate limiting with a token bucket.

Each client key holds up to `burst` tokens, refilled at `rate_per_minute / 60` per second; a request
spends one. The clock is passed in (`now`) so behaviour is deterministic under test and the class
never reads wall time itself.

Memory is bounded: a bucket that has been idle long enough to refill completely is
indistinguishable from a client never seen before, so it is dropped. A sweep runs at most once per
full-refill interval, which keeps `allow` amortised O(1) while the table cannot outgrow the clients
active within one such interval. Not thread-safe; call it from one event loop or guard it.
"""
from __future__ import annotations

from typing import Dict, Tuple

__all__ = ["TokenBucket"]


class TokenBucket:
    """Token bucket keyed by client; see the module docstring."""

    def __init__(self, rate_per_minute: float, burst: int) -> None:
        if rate_per_minute <= 0 or burst <= 0:
            raise ValueError("rate_per_minute and burst must be positive")
        self._rate = rate_per_minute / 60.0
        self._burst = float(burst)
        self._full_after = self._burst / self._rate
        self._buckets: Dict[str, Tuple[float, float]] = {}
        self._last_sweep: float = 0.0
        self._swept = False

    def __len__(self) -> int:
        return len(self._buckets)

    def allow(self, key: str, now: float) -> Tuple[bool, float]:
        """Spend a token for `key`; return (allowed, seconds until a token is available)."""
        self._maybe_sweep(now)
        tokens, last = self._buckets.get(key, (self._burst, now))
        tokens = min(self._burst, tokens + max(0.0, now - last) * self._rate)
        if tokens >= 1.0:
            self._buckets[key] = (tokens - 1.0, now)
            return True, 0.0
        self._buckets[key] = (tokens, now)
        return False, (1.0 - tokens) / self._rate

    def _maybe_sweep(self, now: float) -> None:
        if not self._swept:
            self._swept, self._last_sweep = True, now
            return
        if now - self._last_sweep < self._full_after:
            return
        self._last_sweep = now
        cutoff = now - self._full_after
        stale = [k for k, (_, last) in self._buckets.items() if last <= cutoff]
        for k in stale:
            del self._buckets[k]
