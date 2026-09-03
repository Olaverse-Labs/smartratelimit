"""Tests for multi-process rate limiting."""

import multiprocessing
import os
import tempfile
import time
from datetime import datetime, timedelta

import pytest

# The library stores naive-UTC timestamps; using its own clock helpers
# keeps these fixtures on exactly the same footing as the code under test.
from smartratelimit._time import utcnow
from smartratelimit import RateLimiter
from smartratelimit.models import RateLimit
from smartratelimit.storage import SQLiteStorage, RedisStorage


def redis_available():
    """Check if Redis is available."""
    try:
        import redis
        client = redis.from_url("redis://localhost:6379/0")
        client.ping()
        return True
    except (ImportError, Exception):
        return False


def write_worker_sqlite(db_path, i):
    """Worker function for SQLite concurrent access test (module-level for pickling)."""
    storage = SQLiteStorage(db_path)
    reset_time = utcnow() + timedelta(hours=1)
    rate_limit = RateLimit(
        endpoint=f"https://api{i}.com",
        limit=100,
        remaining=50,
        reset_time=reset_time,
        window=timedelta(hours=1),
    )
    storage.set_rate_limit(f"https://api{i}.com", rate_limit)


def worker_process_sqlite(db_path, endpoint, num_requests, results_queue):
    """Worker process for SQLite multi-process test."""
    try:
        limiter = RateLimiter(storage=f"sqlite:///{db_path}")
        limiter.set_limit(endpoint, limit=10, window="1m")

        success_count = 0
        for _ in range(num_requests):
            # Simulate a request by checking if we can consume a token
            status = limiter.get_status(endpoint)
            if status and status.remaining > 0:
                # Update remaining (simulating request)
                rate_limit = limiter._storage.get_rate_limit(
                    limiter._get_endpoint_key(f"https://{endpoint}")
                )
                if rate_limit:
                    rate_limit.remaining -= 1
                    limiter._storage.set_rate_limit(
                        limiter._get_endpoint_key(f"https://{endpoint}"), rate_limit
                    )
                    success_count += 1
            time.sleep(0.01)  # Small delay

        results_queue.put(success_count)
    except Exception as e:
        results_queue.put(f"ERROR: {e}")


def worker_process_redis(redis_url, endpoint, num_requests, results_queue):
    """Worker process for Redis multi-process test."""
    try:
        limiter = RateLimiter(storage=redis_url)
        limiter.set_limit(endpoint, limit=10, window="1m")

        success_count = 0
        for _ in range(num_requests):
            # Simulate a request by checking if we can consume a token
            status = limiter.get_status(endpoint)
            if status and status.remaining > 0:
                # Update remaining (simulating request)
                rate_limit = limiter._storage.get_rate_limit(
                    limiter._get_endpoint_key(f"https://{endpoint}")
                )
                if rate_limit:
                    rate_limit.remaining -= 1
                    limiter._storage.set_rate_limit(
                        limiter._get_endpoint_key(f"https://{endpoint}"), rate_limit
                    )
                    success_count += 1
            time.sleep(0.01)  # Small delay

        results_queue.put(success_count)
    except Exception as e:
        results_queue.put(f"ERROR: {e}")


class TestMultiProcess:
    """Test multi-process rate limiting."""

    def test_sqlite_concurrent_access(self):
        """Test SQLite storage with concurrent access."""
        with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as f:
            db_path = f.name

        try:
            storage = SQLiteStorage(db_path)

            # Test concurrent writes using module-level function
            processes = []
            for i in range(5):
                p = multiprocessing.Process(target=write_worker_sqlite, args=(db_path, i))
                p.start()
                processes.append(p)

            for p in processes:
                p.join()

            # Verify all writes succeeded
            for i in range(5):
                result = storage.get_rate_limit(f"https://api{i}.com")
                assert result is not None
                assert result.limit == 100

        finally:
            if os.path.exists(db_path):
                os.unlink(db_path)

    @pytest.mark.skipif(not redis_available(), reason="Redis not available")
    def test_redis_shared_state(self):
        """Test that Redis shares state across processes."""
        redis_url = "redis://localhost:6379/0"
        # Use a unique endpoint name to avoid conflicts with other tests
        test_id = f"test{int(time.time())}"
        
        try:
            # Clean up any existing test keys
            import redis
            client = redis.from_url(redis_url)
            for key in client.scan_iter(match="ratelimit:*"):
                key_str = key.decode('utf-8') if isinstance(key, bytes) else key
                if test_id in key_str:
                    client.delete(key)
            
            # Test that rate limits are actually shared by having multiple processes
            # consume from the same endpoint
            endpoint = f"{test_id}.shared.api.com"
            limiter = RateLimiter(storage=redis_url)
            limiter.set_limit(endpoint, limit=20, window="1m")
            
            # Initialize the rate limit with full remaining count
            initial_limit = limiter._storage.get_rate_limit(
                limiter._get_endpoint_key(f"https://{endpoint}")
            )
            if initial_limit:
                initial_limit.remaining = 20
                limiter._storage.set_rate_limit(
                    limiter._get_endpoint_key(f"https://{endpoint}"), initial_limit
                )
            
            # Verify initial state
            initial_status = limiter.get_status(endpoint)
            assert initial_status is not None, "Initial status should exist"
            assert initial_status.remaining == 20, f"Initial remaining should be 20, got {initial_status.remaining}"
            
            # Have multiple processes consume tokens
            results_queue = multiprocessing.Queue()
            processes = []
            for _ in range(3):
                p = multiprocessing.Process(
                    target=worker_process_redis,
                    args=(redis_url, endpoint, 5, results_queue)
                )
                p.start()
                processes.append(p)
            
            for p in processes:
                p.join()
            
            # Collect results
            total_consumed = 0
            while not results_queue.empty():
                result = results_queue.get()
                if isinstance(result, int):
                    total_consumed += result
                elif isinstance(result, str) and result.startswith("ERROR"):
                    pytest.fail(f"Worker process error: {result}")
            
            # Verify that the remaining count reflects shared consumption
            final_status = limiter.get_status(endpoint)
            assert final_status is not None, "Final status should exist"
            # The remaining should be less than initial due to shared consumption
            # Each process tried to consume 5, so at least some consumption should have happened
            assert final_status.remaining < 20, f"Remaining should be less than 20, got {final_status.remaining}"
            assert total_consumed > 0, "At least some tokens should have been consumed"
            
        finally:
            # Clean up test keys
            try:
                import redis
                client = redis.from_url(redis_url)
                for key in client.scan_iter(match="ratelimit:*"):
                    key_str = key.decode('utf-8') if isinstance(key, bytes) else key
                    if test_id in key_str:
                        client.delete(key)
            except Exception:
                pass

