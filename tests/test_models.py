"""Tests for data models."""

from datetime import datetime, timedelta
from time import sleep

import pytest

# The library stores naive-UTC timestamps; using its own clock helpers
# keeps these fixtures on exactly the same footing as the code under test.
from smartratelimit._time import utcnow
from smartratelimit.models import RateLimit, RateLimitStatus, TokenBucket


class TestTokenBucket:
    """Test TokenBucket implementation."""

    def test_initial_state(self):
        """Test bucket starts with full capacity."""
        bucket = TokenBucket(capacity=10.0, tokens=10.0, refill_rate=1.0)
        assert bucket.tokens == 10.0
        assert bucket.capacity == 10.0
        assert bucket.refill_rate == 1.0

    def test_consume_success(self):
        """Test successful token consumption."""
        bucket = TokenBucket(capacity=10.0, tokens=10.0, refill_rate=1.0)
        assert bucket.consume(5.0) is True
        assert bucket.tokens == 5.0

    def test_consume_failure(self):
        """Test failed token consumption when insufficient tokens."""
        bucket = TokenBucket(capacity=10.0, tokens=2.0, refill_rate=1.0)
        # Freeze time to prevent refill during consume
        from datetime import datetime
        now = utcnow()
        bucket.last_update = now
        assert bucket.consume(5.0, now=now) is False
        # Tokens should remain unchanged (or slightly more due to refill)
        assert bucket.tokens <= 2.1  # Allow small tolerance

    def test_refill(self):
        """Test token refill over time."""
        bucket = TokenBucket(capacity=10.0, tokens=5.0, refill_rate=2.0)
        initial_time = utcnow()
        bucket.last_update = initial_time
        later_time = initial_time + timedelta(seconds=2)

        bucket.refill(later_time)
        assert abs(bucket.tokens - 9.0) < 0.1  # 5 + (2 * 2) = 9, allow small tolerance

    def test_refill_capacity_limit(self):
        """Test refill doesn't exceed capacity."""
        bucket = TokenBucket(capacity=10.0, tokens=9.0, refill_rate=5.0)
        initial_time = utcnow()
        later_time = initial_time + timedelta(seconds=1)

        bucket.refill(later_time)
        assert bucket.tokens == 10.0  # Capped at capacity

    def test_wait_time(self):
        """Test wait time calculation."""
        bucket = TokenBucket(capacity=10.0, tokens=2.0, refill_rate=2.0)
        # Freeze time for consistent calculation
        from datetime import datetime
        now = utcnow()
        bucket.last_update = now
        wait = bucket.wait_time(5.0, now=now)
        assert abs(wait - 1.5) < 0.1  # Need 3 more tokens, at 2/sec = 1.5 seconds

    def test_wait_time_zero(self):
        """Test wait time is zero when tokens available."""
        bucket = TokenBucket(capacity=10.0, tokens=10.0, refill_rate=1.0)
        wait = bucket.wait_time(5.0)
        assert wait == 0.0

    def test_reset(self):
        """Test bucket reset."""
        bucket = TokenBucket(capacity=10.0, tokens=2.0, refill_rate=1.0)
        bucket.reset()
        assert bucket.tokens == 10.0


class TestRateLimitStatus:
    """Test RateLimitStatus model."""

    def test_basic_properties(self):
        """Test basic status properties."""
        reset_time = utcnow() + timedelta(seconds=60)
        status = RateLimitStatus(
            endpoint="https://api.example.com",
            limit=100,
            remaining=50,
            reset_time=reset_time,
            window=timedelta(minutes=1),
        )

        assert status.endpoint == "https://api.example.com"
        assert status.limit == 100
        assert status.remaining == 50
        assert status.is_exceeded is False
        assert status.utilization == 0.5

    def test_is_exceeded(self):
        """Test is_exceeded property."""
        status = RateLimitStatus(
            endpoint="https://api.example.com",
            limit=100,
            remaining=0,
        )
        assert status.is_exceeded is True

    def test_utilization(self):
        """Test utilization calculation."""
        status = RateLimitStatus(
            endpoint="https://api.example.com",
            limit=100,
            remaining=25,
        )
        assert status.utilization == 0.75

    def test_reset_in(self):
        """Test reset_in calculation."""
        reset_time = utcnow() + timedelta(seconds=45)
        status = RateLimitStatus(
            endpoint="https://api.example.com",
            limit=100,
            remaining=50,
            reset_time=reset_time,
        )
        assert 40 < status.reset_in < 50  # Allow some tolerance

    def test_reset_in_none(self):
        """Test reset_in when reset_time is None."""
        status = RateLimitStatus(
            endpoint="https://api.example.com",
            limit=100,
            remaining=50,
            reset_time=None,
        )
        assert status.reset_in is None


class TestRateLimit:
    """Test RateLimit model."""

    def test_to_status(self):
        """Test conversion to RateLimitStatus."""
        reset_time = utcnow() + timedelta(minutes=1)
        rate_limit = RateLimit(
            endpoint="https://api.example.com",
            limit=100,
            remaining=50,
            reset_time=reset_time,
            window=timedelta(minutes=1),
        )

        status = rate_limit.to_status()
        assert isinstance(status, RateLimitStatus)
        assert status.endpoint == "https://api.example.com"
        assert status.limit == 100
        assert status.remaining == 50

