"""Data models for rate limit tracking."""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional


@dataclass
class RateLimitStatus:
    """Status information about current rate limits for an endpoint."""

    endpoint: str
    limit: int
    remaining: int
    reset_time: Optional[datetime] = None
    window: Optional[timedelta] = None
    confidence: str = "confirmed"
    """How this limit was arrived at.

    ``'confirmed'``  the API reported both the limit and its window.
    ``'estimated'``  the API reported a limit but no usable reset, so the
                     window was assumed and may be wrong by orders of
                     magnitude. Configure the real limit with
                     :meth:`RateLimiter.set_limit` if it matters.
    ``'configured'`` set explicitly by the caller.
    """

    @property
    def reset_in(self) -> Optional[float]:
        """Get seconds until rate limit resets."""
        if self.reset_time is None:
            return None
        delta = self.reset_time - datetime.utcnow()
        return max(0, delta.total_seconds())

    @property
    def is_exceeded(self) -> bool:
        """Check if rate limit is currently exceeded."""
        return self.remaining <= 0

    @property
    def utilization(self) -> float:
        """Get utilization percentage (0.0 to 1.0)."""
        if self.limit == 0:
            return 1.0
        return 1.0 - (self.remaining / self.limit)


@dataclass
class RateLimit:
    """Internal rate limit tracking data."""

    endpoint: str
    limit: int
    remaining: int
    reset_time: datetime
    window: timedelta
    last_updated: datetime = field(default_factory=datetime.utcnow)
    confidence: str = "confirmed"

    def to_status(self) -> RateLimitStatus:
        """Convert to public status object."""
        return RateLimitStatus(
            endpoint=self.endpoint,
            limit=self.limit,
            remaining=self.remaining,
            reset_time=self.reset_time,
            window=self.window,
            confidence=self.confidence,
        )


@dataclass
class TokenBucket:
    """Token bucket for rate limiting."""

    capacity: float
    tokens: float
    refill_rate: float  # tokens per second
    last_update: datetime = field(default_factory=datetime.utcnow)

    def refill(self, now: Optional[datetime] = None) -> None:
        """Refill tokens based on elapsed time."""
        if now is None:
            now = datetime.utcnow()

        elapsed = (now - self.last_update).total_seconds()
        if elapsed <= 0:
            return

        # Add tokens based on refill rate
        self.tokens = min(self.capacity, self.tokens + (elapsed * self.refill_rate))
        self.last_update = now

    def consume(self, tokens: float = 1.0, now: Optional[datetime] = None) -> bool:
        """Try to consume tokens. Returns True if successful."""
        self.refill(now)
        if self.tokens >= tokens:
            self.tokens -= tokens
            return True
        return False

    def wait_time(self, tokens: float = 1.0, now: Optional[datetime] = None) -> float:
        """Calculate how long to wait before tokens are available."""
        if now is None:
            now = datetime.utcnow()

        self.refill(now)
        if self.tokens >= tokens:
            return 0.0

        needed = tokens - self.tokens
        if self.refill_rate <= 0:
            return float("inf")

        return needed / self.refill_rate

    def reset(self) -> None:
        """Reset bucket to full capacity."""
        self.tokens = self.capacity
        self.last_update = datetime.utcnow()

