"""Tests for SQLite storage backend."""

import os
import tempfile
from datetime import datetime, timedelta

import pytest

# The library stores naive-UTC timestamps; using its own clock helpers
# keeps these fixtures on exactly the same footing as the code under test.
from smartratelimit._time import utcnow
from smartratelimit.models import RateLimit, TokenBucket
from smartratelimit.storage import SQLiteStorage


class TestSQLiteStorage:
    """Test SQLiteStorage backend."""

    def test_init_memory(self):
        """Test initialization with in-memory database."""
        storage = SQLiteStorage(":memory:")
        assert storage.db_path == ":memory:"

    def test_init_file(self):
        """Test initialization with file database."""
        with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as f:
            db_path = f.name

        try:
            storage = SQLiteStorage(db_path)
            assert storage.db_path == db_path
        finally:
            if os.path.exists(db_path):
                os.unlink(db_path)

    def test_get_set_rate_limit(self):
        """Test storing and retrieving rate limits."""
        storage = SQLiteStorage(":memory:")

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

    def test_get_nonexistent_rate_limit(self):
        """Test retrieving non-existent rate limit."""
        storage = SQLiteStorage(":memory:")
        result = storage.get_rate_limit("https://api.example.com")
        assert result is None

    def test_get_set_token_bucket(self):
        """Test storing and retrieving token buckets."""
        storage = SQLiteStorage(":memory:")

        bucket = TokenBucket(capacity=10.0, tokens=5.0, refill_rate=1.0)
        storage.set_token_bucket("test_key", bucket)

        retrieved = storage.get_token_bucket("test_key")
        assert retrieved is not None
        assert retrieved.capacity == bucket.capacity
        assert retrieved.tokens == bucket.tokens
        assert retrieved.refill_rate == bucket.refill_rate

    def test_persistence(self):
        """Test that data persists across storage instances."""
        with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as f:
            db_path = f.name

        try:
            # Create storage and add data
            storage1 = SQLiteStorage(db_path)
            reset_time = utcnow() + timedelta(hours=1)
            rate_limit = RateLimit(
                endpoint="https://api.example.com",
                limit=100,
                remaining=50,
                reset_time=reset_time,
                window=timedelta(hours=1),
            )
            storage1.set_rate_limit("https://api.example.com", rate_limit)

            # Create new storage instance with same DB
            storage2 = SQLiteStorage(db_path)
            retrieved = storage2.get_rate_limit("https://api.example.com")

            assert retrieved is not None
            assert retrieved.limit == 100
            assert retrieved.remaining == 50
        finally:
            if os.path.exists(db_path):
                os.unlink(db_path)

    def test_clear_specific_endpoint(self):
        """Test clearing specific endpoint."""
        storage = SQLiteStorage(":memory:")

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

    def test_clear_all(self):
        """Test clearing all data."""
        storage = SQLiteStorage(":memory:")

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

    def test_thread_safety(self):
        """Test thread safety of storage operations."""
        import threading

        storage = SQLiteStorage(":memory:")
        errors = []

        def worker():
            try:
                for i in range(50):
                    reset_time = utcnow() + timedelta(hours=1)
                    rate_limit = RateLimit(
                        endpoint=f"https://api{i}.com",
                        limit=100,
                        remaining=50,
                        reset_time=reset_time,
                        window=timedelta(hours=1),
                    )
                    storage.set_rate_limit(f"https://api{i}.com", rate_limit)
                    storage.get_rate_limit(f"https://api{i}.com")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0

