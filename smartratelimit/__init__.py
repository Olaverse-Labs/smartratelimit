"""
smartratelimit: Automatic API rate limit management for Python.

A drop-in solution that automatically detects, tracks, and respects API rate limits
across multiple processes and application restarts.
"""

from smartratelimit.async_client import AsyncRateLimiter
from smartratelimit.core import RateLimiter, RateLimitExceeded
from smartratelimit.metrics import MetricsCollector
from smartratelimit.models import RateLimitStatus
from smartratelimit.retry import RetryConfig, RetryHandler, RetryStrategy

__version__ = "0.4.0"
__all__ = [
    "RateLimiter",
    "AsyncRateLimiter",
    "RateLimitStatus",
    "RateLimitExceeded",
    "RetryConfig",
    "RetryHandler",
    "RetryStrategy",
    "MetricsCollector",
]
