"""Performance benchmarks for smartratelimit."""

import time
from datetime import datetime, timedelta

# The library stores naive-UTC timestamps; using its own clock helpers
# keeps these fixtures on exactly the same footing as the code under test.
from smartratelimit._time import utcnow
from smartratelimit import RateLimiter
from smartratelimit.models import RateLimit, TokenBucket
from smartratelimit.storage import MemoryStorage, SQLiteStorage


def benchmark_memory_storage():
    """Benchmark in-memory storage operations."""
    storage = MemoryStorage()
    reset_time = utcnow() + timedelta(hours=1)

    # Benchmark set_rate_limit
    start = time.perf_counter()
    for i in range(1000):
        rate_limit = RateLimit(
            endpoint=f"https://api{i}.com",
            limit=100,
            remaining=50,
            reset_time=reset_time,
            window=timedelta(hours=1),
        )
        storage.set_rate_limit(f"https://api{i}.com", rate_limit)
    set_time = time.perf_counter() - start

    # Benchmark get_rate_limit
    start = time.perf_counter()
    for i in range(1000):
        storage.get_rate_limit(f"https://api{i}.com")
    get_time = time.perf_counter() - start

    # Benchmark token bucket operations
    start = time.perf_counter()
    for i in range(1000):
        bucket = TokenBucket(capacity=10.0, tokens=5.0, refill_rate=1.0)
        storage.set_token_bucket(f"bucket{i}", bucket)
        storage.get_token_bucket(f"bucket{i}")
    bucket_time = time.perf_counter() - start

    print("Memory Storage Benchmarks:")
    print(f"  set_rate_limit (1000 ops): {set_time*1000:.2f}ms ({set_time/1000*1e6:.2f}μs per op)")
    print(f"  get_rate_limit (1000 ops): {get_time*1000:.2f}ms ({get_time/1000*1e6:.2f}μs per op)")
    print(f"  token_bucket ops (1000 ops): {bucket_time*1000:.2f}ms ({bucket_time/1000*1e6:.2f}μs per op)")


def benchmark_sqlite_storage():
    """Benchmark SQLite storage operations."""
    storage = SQLiteStorage(":memory:")
    reset_time = utcnow() + timedelta(hours=1)

    # Benchmark set_rate_limit
    start = time.perf_counter()
    for i in range(1000):
        rate_limit = RateLimit(
            endpoint=f"https://api{i}.com",
            limit=100,
            remaining=50,
            reset_time=reset_time,
            window=timedelta(hours=1),
        )
        storage.set_rate_limit(f"https://api{i}.com", rate_limit)
    set_time = time.perf_counter() - start

    # Benchmark get_rate_limit
    start = time.perf_counter()
    for i in range(1000):
        storage.get_rate_limit(f"https://api{i}.com")
    get_time = time.perf_counter() - start

    # Benchmark token bucket operations
    start = time.perf_counter()
    for i in range(1000):
        bucket = TokenBucket(capacity=10.0, tokens=5.0, refill_rate=1.0)
        storage.set_token_bucket(f"bucket{i}", bucket)
        storage.get_token_bucket(f"bucket{i}")
    bucket_time = time.perf_counter() - start

    print("\nSQLite Storage Benchmarks:")
    print(f"  set_rate_limit (1000 ops): {set_time*1000:.2f}ms ({set_time/1000*1e6:.2f}μs per op)")
    print(f"  get_rate_limit (1000 ops): {get_time*1000:.2f}ms ({get_time/1000*1e6:.2f}μs per op)")
    print(f"  token_bucket ops (1000 ops): {bucket_time*1000:.2f}ms ({bucket_time/1000*1e6:.2f}μs per op)")


def benchmark_rate_limiter_overhead():
    """Benchmark rate limiter overhead per request."""
    limiter = RateLimiter()
    limiter.set_limit("api.example.com", limit=10000, window="1m")

    # Simulate requests (without actual HTTP calls)
    start = time.perf_counter()
    for _ in range(1000):
        # Simulate the rate limiting check
        status = limiter.get_status("api.example.com")
        if status:
            bucket = limiter._storage.get_token_bucket(
                limiter._get_bucket_key("https://api.example.com")
            )
            if bucket:
                bucket.refill()
                bucket.consume()
    overhead_time = time.perf_counter() - start

    print("\nRate Limiter Overhead:")
    print(f"  Per-request overhead (1000 ops): {overhead_time*1000:.2f}ms ({overhead_time/1000*1e6:.2f}μs per op)")


if __name__ == "__main__":
    print("Running performance benchmarks...\n")
    benchmark_memory_storage()
    benchmark_sqlite_storage()
    benchmark_rate_limiter_overhead()
    print("\nBenchmarks completed!")

