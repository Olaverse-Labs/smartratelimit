"""Tests for atomic token accounting in the storage backends.

These cover the property the library's "multi-process safe" claim rests on: a
bucket must not hand out more tokens than it holds, no matter how many threads
or processes ask at once.
"""

import multiprocessing
import os
import tempfile
import threading
from datetime import timedelta

import pytest

from smartratelimit.storage import MemoryStorage, RedisStorage, SQLiteStorage

REDIS_URL = "redis://localhost:6379/0"
REDIS_PREFIX = "smartratelimit-test:"


def redis_available():
    try:
        import redis

        redis.from_url(REDIS_URL).ping()
        return True
    except Exception:
        return False


requires_redis = pytest.mark.skipif(
    not redis_available(), reason="Redis not available"
)

# A day-long window: refill over the length of a test is negligible, so any
# extra grant is a lost update rather than a legitimately replenished token.
SLOW_WINDOW_SECONDS = 86400.0


def _make_sqlite_path():
    return os.path.join(tempfile.mkdtemp(), "buckets.db")


def _redis_worker(capacity, attempts, queue):
    """Module-level so it can be pickled for spawn-based start methods."""
    storage = RedisStorage(redis_url=REDIS_URL, key_prefix=REDIS_PREFIX)
    granted = 0
    for _ in range(attempts):
        allowed, _ = storage.acquire(
            "shared", capacity, capacity / SLOW_WINDOW_SECONDS
        )
        if allowed:
            granted += 1
    queue.put(granted)


def _sqlite_worker(db_path, capacity, attempts, queue):
    """Module-level so it can be pickled for spawn-based start methods."""
    storage = SQLiteStorage(db_path)
    granted = 0
    for _ in range(attempts):
        allowed, _ = storage.acquire(
            "shared", capacity, capacity / SLOW_WINDOW_SECONDS
        )
        if allowed:
            granted += 1
    queue.put(granted)


class TestAcquireSemantics:
    """Behaviour every backend must share."""

    @pytest.fixture(
        params=[
            "memory",
            "sqlite",
            pytest.param("redis", marks=requires_redis),
        ]
    )
    def storage(self, request):
        if request.param == "memory":
            return MemoryStorage()
        if request.param == "sqlite":
            return SQLiteStorage(_make_sqlite_path())

        storage = RedisStorage(redis_url=REDIS_URL, key_prefix=REDIS_PREFIX)
        storage.clear()
        request.addfinalizer(storage.clear)
        return storage

    def test_tokens_are_actually_consumed(self, storage):
        """Consumption survives the round trip through storage."""
        for _ in range(5):
            allowed, wait = storage.acquire("k", 100.0, 100 / 3600.0)
            assert allowed is True
            assert wait == 0.0

        bucket = storage.get_token_bucket("k")
        assert bucket is not None
        assert bucket.tokens == pytest.approx(95.0, abs=0.1)

    def test_bucket_is_exhausted_and_reports_wait(self, storage):
        """Once empty, the bucket refuses and says how long to wait."""
        capacity, refill_rate = 3.0, 3 / 60.0

        for _ in range(3):
            assert storage.acquire("k", capacity, refill_rate)[0] is True

        allowed, wait = storage.acquire("k", capacity, refill_rate)
        assert allowed is False
        # One token at 3-per-minute is 20 seconds away.
        assert wait == pytest.approx(20.0, abs=1.0)

    def test_refused_acquire_consumes_nothing(self, storage):
        """A refusal must not deduct, or the bucket drifts negative."""
        capacity, refill_rate = 2.0, 2 / SLOW_WINDOW_SECONDS

        for _ in range(2):
            storage.acquire("k", capacity, refill_rate)
        for _ in range(5):
            assert storage.acquire("k", capacity, refill_rate)[0] is False

        bucket = storage.get_token_bucket("k")
        assert bucket.tokens >= 0.0
        assert bucket.tokens < 1.0

    def test_lowered_limit_shrinks_the_bucket(self, storage):
        """A limit revised downward must not leave stale tokens behind."""
        storage.acquire("k", 100.0, 100 / SLOW_WINDOW_SECONDS)

        granted = sum(
            storage.acquire("k", 5.0, 5 / SLOW_WINDOW_SECONDS)[0] for _ in range(10)
        )
        assert granted == 5

    def test_concurrent_threads_cannot_overdraw(self, storage):
        """The classic read-modify-write race: 8 threads, one bucket."""
        capacity = 50.0
        granted = []
        lock = threading.Lock()

        def worker():
            local = 0
            for _ in range(25):
                allowed, _ = storage.acquire(
                    "shared", capacity, capacity / SLOW_WINDOW_SECONDS
                )
                if allowed:
                    local += 1
            with lock:
                granted.append(local)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert sum(granted) == 50


class TestSQLiteMultiProcess:
    """SQLite is the backend people reach for to share a limit between workers."""

    def test_concurrent_processes_cannot_overdraw(self):
        """200 attempts across 8 processes must yield exactly 50 grants."""
        db_path = _make_sqlite_path()
        SQLiteStorage(db_path)  # create the schema before the workers race

        capacity = 50.0
        queue = multiprocessing.Queue()
        processes = [
            multiprocessing.Process(
                target=_sqlite_worker, args=(db_path, capacity, 25, queue)
            )
            for _ in range(8)
        ]
        for p in processes:
            p.start()

        total = sum(queue.get() for _ in processes)
        for p in processes:
            p.join()

        assert total == 50


@requires_redis
class TestRedisMultiProcess:
    """Redis is the backend the README points at for distributed limiting."""

    def test_concurrent_processes_cannot_overdraw(self):
        """200 attempts across 8 processes must yield exactly 50 grants."""
        capacity = 50.0
        storage = RedisStorage(redis_url=REDIS_URL, key_prefix=REDIS_PREFIX)
        storage.clear()

        try:
            queue = multiprocessing.Queue()
            processes = [
                multiprocessing.Process(
                    target=_redis_worker, args=(capacity, 25, queue)
                )
                for _ in range(8)
            ]
            for p in processes:
                p.start()

            total = sum(queue.get() for _ in processes)
            for p in processes:
                p.join()

            assert total == 50
        finally:
            storage.clear()


class TestAsyncAcquire:
    """The async limiter must share the same atomic accounting."""

    async def test_async_acquire_drains_the_bucket(self):
        from smartratelimit import AsyncRateLimiter

        limiter = AsyncRateLimiter()
        limiter.set_limit("api.example.com", limit=5, window="1h")

        for _ in range(5):
            await limiter._acquire("https://api.example.com", 5, timedelta(hours=1))

        bucket = limiter._storage.get_token_bucket("https://api.example.com:default")
        assert bucket.tokens == pytest.approx(0.0, abs=0.01)

    async def test_async_acquire_raises_when_saturated(self):
        from smartratelimit import AsyncRateLimiter
        from smartratelimit.core import RateLimitExceeded

        limiter = AsyncRateLimiter(raise_on_limit=True)
        window = timedelta(hours=1)

        await limiter._acquire("https://api.example.com", 1, window)

        with pytest.raises(RateLimitExceeded):
            await limiter._acquire("https://api.example.com", 1, window)
