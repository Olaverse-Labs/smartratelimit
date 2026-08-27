"""Data models for rate limit tracking."""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, Iterator, Optional

from smartratelimit._time import utcnow

#: The dimension every API limits: one unit spent per request.
REQUESTS = "requests"


@dataclass
class RateLimitStatus:
    """Status information about current rate limits for an endpoint."""

    endpoint: str
    limit: int
    remaining: int
    reset_time: Optional[datetime] = None
    window: Optional[timedelta] = None
    confidence: str = "confirmed"
    dimensions: Dict[str, "LimitDimension"] = field(default_factory=dict)
    """Every metered dimension keyed by name, including ``requests``."""

    _CONFIDENCE_DOC = """How ``confidence`` is arrived at.

    ``'confirmed'``  the API reported both the limit and its window.
    ``'estimated'``  the API reported a limit but no usable reset, so the
                     window was assumed and may be wrong by orders of
                     magnitude. Configure the real limit with
                     :meth:`RateLimiter.set_limit` if it matters.
    ``'configured'`` set explicitly by the caller.
    ``'registry'``   seeded from a built-in provider profile, and replaced as
                     soon as the API reports its own numbers.
    """

    @property
    def reset_in(self) -> Optional[float]:
        """Get seconds until rate limit resets."""
        if self.reset_time is None:
            return None
        delta = self.reset_time - utcnow()
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
class LimitDimension:
    """
    One axis of a rate limit.

    Most APIs meter more than requests. OpenAI's binding constraint is usually
    tokens per minute, not requests per minute, and both apply at once -- a
    request well inside the request budget can still be refused for tokens. Each
    dimension gets its own budget and its own bucket, and a request has to
    satisfy all of them to go out.
    """

    name: str
    limit: int
    remaining: int
    reset_time: datetime
    window: timedelta
    confidence: str = "confirmed"


@dataclass
class RateLimit:
    """
    Internal rate limit tracking data for one endpoint scope.

    The flat fields describe the ``requests`` dimension, which every API has and
    which stored rows from earlier versions expect. Anything else the API meters
    -- tokens per minute, requests per day -- lives in :attr:`dimensions`. Use
    :meth:`all_dimensions` to walk them uniformly.
    """

    endpoint: str
    limit: int
    remaining: int
    reset_time: datetime
    window: timedelta
    last_updated: datetime = field(default_factory=utcnow)
    confidence: str = "confirmed"
    dimensions: Dict[str, LimitDimension] = field(default_factory=dict)
    """Dimensions beyond ``requests``, keyed by name."""

    def all_dimensions(self) -> Iterator[LimitDimension]:
        """Yield every dimension, ``requests`` first."""
        yield LimitDimension(
            name=REQUESTS,
            limit=self.limit,
            remaining=self.remaining,
            reset_time=self.reset_time,
            window=self.window,
            confidence=self.confidence,
        )
        for dimension in self.dimensions.values():
            yield dimension

    def dimension(self, name: str) -> Optional[LimitDimension]:
        """Get one dimension by name, or None if this scope does not meter it."""
        if name == REQUESTS:
            return next(self.all_dimensions())
        return self.dimensions.get(name)

    def with_dimension(self, dimension: LimitDimension) -> "RateLimit":
        """Return a copy with ``dimension`` added or replaced."""
        if dimension.name == REQUESTS:
            return RateLimit(
                endpoint=self.endpoint,
                limit=dimension.limit,
                remaining=dimension.remaining,
                reset_time=dimension.reset_time,
                window=dimension.window,
                confidence=dimension.confidence,
                dimensions=dict(self.dimensions),
            )

        merged = dict(self.dimensions)
        merged[dimension.name] = dimension
        return RateLimit(
            endpoint=self.endpoint,
            limit=self.limit,
            remaining=self.remaining,
            reset_time=self.reset_time,
            window=self.window,
            confidence=self.confidence,
            dimensions=merged,
        )

    def to_status(self) -> RateLimitStatus:
        """Convert to public status object."""
        return RateLimitStatus(
            endpoint=self.endpoint,
            limit=self.limit,
            remaining=self.remaining,
            reset_time=self.reset_time,
            window=self.window,
            confidence=self.confidence,
            dimensions={d.name: d for d in self.all_dimensions()},
        )


@dataclass
class TokenBucket:
    """Token bucket for rate limiting."""

    capacity: float
    tokens: float
    refill_rate: float  # tokens per second
    last_update: datetime = field(default_factory=utcnow)

    def refill(self, now: Optional[datetime] = None) -> None:
        """Refill tokens based on elapsed time."""
        if now is None:
            now = utcnow()

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
            now = utcnow()

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
        self.last_update = utcnow()

