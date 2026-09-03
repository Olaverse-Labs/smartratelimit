"""
smartratelimit: Automatic API rate limit management for Python.

A drop-in solution that automatically detects, tracks, and respects API rate limits
across multiple processes and application restarts.
"""

from smartratelimit.async_client import AsyncRateLimiter
from smartratelimit.core import RateLimiter, RateLimitExceeded, StorageUnavailable
from smartratelimit.metrics import MetricsCollector
from smartratelimit.models import LimitDimension, RateLimitStatus
from smartratelimit.providers import Provider, SeedLimit, register_provider
from smartratelimit.retry import RetryConfig, RetryHandler, RetryStrategy

__version__ = "0.5.1"
__all__ = [
    "RateLimiter",
    "AsyncRateLimiter",
    "RateLimitStatus",
    "LimitDimension",
    "Provider",
    "SeedLimit",
    "register_provider",
    "RateLimitExceeded",
    "StorageUnavailable",
    "RetryConfig",
    "RetryHandler",
    "RetryStrategy",
    "MetricsCollector",
]
