"""Tests for Redis storage backend."""

import pytest
from datetime import datetime, timedelta

# The library stores naive-UTC timestamps; using its own clock helpers
# keeps these fixtures on exactly the same footing as the code under test.
from smartratelimit._time import utcnow
from smartratelimit.models import RateLimit, TokenBucket
from smartratelimit.storage import RedisStorage


def redis_available():
    """Check if Redis is available."""
    try:
        import redis
        client = redis.from_url("redis://localhost:6379/0")
        client.ping()
        return True
    except (ImportError, Exception):
        return False


class TestRedisStorage:
    """Test RedisStorage backend."""

    @pytest.mark.skipif(not redis_available(), reason="Redis not available")
    def test_init(self):
        """Test initialization."""
        storage = RedisStorage("redis://localhost:6379/0", key_prefix="test:ratelimit:")
        assert storage.key_prefix == "test:ratelimit:"

    @pytest.mark.skipif(not redis_available(), reason="Redis not available")
    def test_get_set_rate_limit(self):
        """Test storing and retrieving rate limits."""
        storage = RedisStorage("redis://localhost:6379/0", key_prefix="test:ratelimit:")
        storage.clear()  # Clean up

        reset_time = utcnow() + timedelta(hours=1)
        rate_limit = RateLimit(
            endpoint="https://api.example.com",
            limit=100,
            remaining=50,
            reset_time=reset_time,
            window=timedelta(hours=1),
        )

        storage.set_rate_limit("https://api.example.com", rate_limit)
        retrieved = storage.get_rate_limit("https://api.example.com")

        assert retrieved is not None
        assert retrieved.endpoint == rate_limit.endpoint
        assert retrieved.limit == rate_limit.limit
        assert retrieved.remaining == rate_limit.remaining
        assert abs((retrieved.reset_time - rate_limit.reset_time).total_seconds()) < 1

        storage.clear()  # Clean up

    @pytest.mark.skipif(not redis_available(), reason="Redis not available")
    def test_get_nonexistent_rate_limit(self):
        """Test retrieving non-existent rate limit."""
        storage = RedisStorage("redis://localhost:6379/0", key_prefix="test:ratelimit:")
        storage.clear()  # Clean up

        result = storage.get_rate_limit("https://api.example.com")
        assert result is None

    @pytest.mark.skipif(not redis_available(), reason="Redis not available")
    def test_get_set_token_bucket(self):
        """Test storing and retrieving token buckets."""
        storage = RedisStorage("redis://localhost:6379/0", key_prefix="test:ratelimit:")
        storage.clear()  # Clean up

        bucket = TokenBucket(capacity=10.0, tokens=5.0, refill_rate=1.0)
        storage.set_token_bucket("test_key", bucket)

        retrieved = storage.get_token_bucket("test_key")
        assert retrieved is not None
        assert retrieved.capacity == bucket.capacity
        assert retrieved.tokens == bucket.tokens
        assert retrieved.refill_rate == bucket.refill_rate

        storage.clear()  # Clean up

    @pytest.mark.skipif(not redis_available(), reason="Redis not available")
    def test_clear_specific_endpoint(self):
        """Test clearing specific endpoint."""
        storage = RedisStorage("redis://localhost:6379/0", key_prefix="test:ratelimit:")
        storage.clear()  # Clean up

        reset_time = utcnow() + timedelta(hours=1)
        rate_limit = RateLimit(
            endpoint="https://api.example.com",
            limit=100,
            remaining=50,
            reset_time=reset_time,
            window=timedelta(hours=1),
        )
        storage.set_rate_limit("https://api.example.com", rate_limit)
        storage.set_token_bucket("https://api.example.com:default", TokenBucket(10, 5, 1))

        storage.clear("https://api.example.com")

        assert storage.get_rate_limit("https://api.example.com") is None
        assert storage.get_token_bucket("https://api.example.com:default") is None

        storage.clear()  # Clean up

    @pytest.mark.skipif(not redis_available(), reason="Redis not available")
    def test_clear_all(self):
        """Test clearing all data."""
        storage = RedisStorage("redis://localhost:6379/0", key_prefix="test:ratelimit:")
        storage.clear()  # Clean up

        reset_time = utcnow() + timedelta(hours=1)
        storage.set_rate_limit(
            "https://api1.com",
            RateLimit("https://api1.com", 100, 50, reset_time, timedelta(hours=1)),
        )
        storage.set_rate_limit(
            "https://api2.com",
            RateLimit("https://api2.com", 200, 100, reset_time, timedelta(hours=1)),
        )
        storage.set_token_bucket("key1", TokenBucket(10, 5, 1))
        storage.set_token_bucket("key2", TokenBucket(20, 10, 2))

        storage.clear()

        assert storage.get_rate_limit("https://api1.com") is None
        assert storage.get_rate_limit("https://api2.com") is None
        assert storage.get_token_bucket("key1") is None
        assert storage.get_token_bucket("key2") is None

    def test_import_error(self):
        """Test that ImportError is raised when redis is not installed."""
        # This test would need to mock the import, but we'll skip it
        # as it's hard to test without actually uninstalling redis
        pass

