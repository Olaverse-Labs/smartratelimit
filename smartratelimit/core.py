"""Core RateLimiter class."""

import logging
import time
from datetime import datetime, timedelta
from typing import Dict, Optional
from urllib.parse import urlparse

import requests

from smartratelimit.detector import RateLimitDetector
from smartratelimit.models import RateLimit, RateLimitStatus, TokenBucket
from smartratelimit.retry import RetryConfig, RetryHandler
from smartratelimit.storage import (
    MemoryStorage,
    RedisStorage,
    SQLiteStorage,
    StorageBackend,
)

logger = logging.getLogger(__name__)


class RateLimitExceeded(Exception):
    """Exception raised when rate limit is exceeded and raise_on_limit=True."""

    pass


class _OriginalRequestSession:
    """
    Minimal session-alike that calls a wrapped session's original ``request``.

    :meth:`RateLimiter.wrap_session` replaces ``session.request`` in place, so
    the limiter needs a handle on the pre-wrap bound method to issue the actual
    call -- otherwise it would call its own wrapper and recurse.
    """

    __slots__ = ("_request",)

    def __init__(self, request):
        self._request = request

    def request(self, method, url, **kwargs):
        return self._request(method, url, **kwargs)


class RateLimiter:
    """
    Main rate limiter class that automatically manages API rate limits.

    Example:
        >>> limiter = RateLimiter()
        >>> response = limiter.request('GET', 'https://api.github.com/users')
        >>> print(response.status_code)
        200
    """

    #: How many times to wait-and-retry for a token before giving up. Each
    #: attempt sleeps for as long as the bucket says it needs, so reaching this
    #: means other workers keep winning the token -- an endpoint saturated
    #: beyond what waiting can fix, not a delay to sit through.
    MAX_WAIT_ATTEMPTS = 64

    def __init__(
        self,
        storage: str = "memory",
        default_limits: Optional[Dict[str, int]] = None,
        headers_map: Optional[Dict[str, str]] = None,
        raise_on_limit: bool = False,
        retry: Optional[RetryConfig] = None,
    ):
        """
        Initialize rate limiter.

        Args:
            storage: Storage backend ('memory', 'sqlite:///path', 'redis://host:port')
            default_limits: Default limits like {'requests_per_second': 10}
            headers_map: Custom header name mapping
            raise_on_limit: If True, raise exception instead of waiting
            retry: How to retry a request the server rejects with 429/503/504.
                Defaults to three attempts with jittered exponential backoff.
        """
        self._storage = self._create_storage(storage)
        self._detector = RateLimitDetector(headers_map)
        self._default_limits = default_limits or {}
        self._raise_on_limit = raise_on_limit
        self._session = requests.Session()
        # Jitter by default: without it every client throttled by the same
        # window wakes up together and collides again on the retry.
        self._retry = RetryHandler(retry or RetryConfig(jitter=0.1))

    def _create_storage(self, storage: str) -> StorageBackend:
        """Create storage backend from string specification."""
        if storage == "memory":
            return MemoryStorage()

        if storage.startswith("sqlite://"):
            db_path = storage.replace("sqlite://", "", 1)
            # Handle different sqlite:// formats
            if db_path.startswith("///"):
                # sqlite:///absolute/path -> /absolute/path
                db_path = db_path[2:]
            elif db_path.startswith("//"):
                # sqlite:////absolute/path (4 slashes) -> /absolute/path
                db_path = db_path[1:]
            elif db_path == "/:memory:":
                # sqlite:///:memory: -> :memory:
                db_path = ":memory:"
            elif db_path.startswith("/"):
                # sqlite:///relative/path -> /relative/path (keep as is)
                pass
            elif not db_path:
                # sqlite:// -> :memory:
                db_path = ":memory:"
            try:
                return SQLiteStorage(db_path=db_path)
            except Exception as e:
                logger.warning(
                    f"Failed to initialize SQLite storage: {e}, falling back to memory"
                )
                return MemoryStorage()

        if storage.startswith("redis://"):
            try:
                return RedisStorage(redis_url=storage)
            except ImportError as e:
                logger.warning(
                    f"Redis package not installed: {e}, falling back to memory"
                )
                return MemoryStorage()
            except Exception as e:
                logger.warning(
                    f"Failed to initialize Redis storage: {e}, falling back to memory"
                )
                return MemoryStorage()

        raise ValueError(f"Unknown storage backend: {storage}")

    @staticmethod
    def _get_endpoint_key(url: str) -> str:
        """Extract endpoint key from URL."""
        parsed = urlparse(url)
        return f"{parsed.scheme}://{parsed.netloc}"

    def _get_bucket_key(self, url: str, limit_type: str = "default") -> str:
        """Get token bucket key for URL."""
        endpoint = self._get_endpoint_key(url)
        return f"{endpoint}:{limit_type}"

    def _acquire(self, url: str, limit: int, window: timedelta) -> None:
        """
        Block until a token for ``url`` has been consumed, or raise.

        Consumption happens inside the storage backend as one atomic step, so
        the count is correct across threads, processes and hosts. Reading a
        bucket here, deducting a token locally and writing it back would lose
        updates -- and with SQLite or Redis it would also lose the deduction
        entirely, since those backends hand back a fresh object each read.

        Args:
            url: Request URL (only its scheme and host are used).
            limit: Requests allowed per window.
            window: Length of the window.

        Raises:
            RateLimitExceeded: If ``raise_on_limit`` is set and no token is
                available, or if the bucket can never refill.
        """
        window_seconds = window.total_seconds() if window else 0.0
        if limit <= 0 or window_seconds <= 0:
            return

        capacity = float(limit)
        refill_rate = capacity / window_seconds
        key = self._get_bucket_key(url)

        for _ in range(self.MAX_WAIT_ATTEMPTS):
            allowed, wait_time = self._storage.acquire(key, capacity, refill_rate)
            if allowed:
                return

            if self._raise_on_limit:
                raise RateLimitExceeded(
                    f"Rate limit exceeded for {url}. Wait {wait_time:.2f} seconds."
                )

            if wait_time == float("inf"):
                raise RateLimitExceeded(
                    f"Rate limit for {url} can never be satisfied: "
                    f"the bucket does not refill."
                )

            logger.info(
                f"Rate limit reached for {url}, waiting {wait_time:.2f} seconds"
            )
            # Another worker may take the token we waited for, so loop rather
            # than assuming one wait is enough.
            time.sleep(max(wait_time, 0.001))

        raise RateLimitExceeded(
            f"Could not acquire a token for {url} after "
            f"{self.MAX_WAIT_ATTEMPTS} attempts; the endpoint is saturated."
        )

    def _get_or_create_bucket(
        self, url: str, limit: int, window: timedelta
    ) -> TokenBucket:
        """
        Get or create the token bucket for URL.

        Deprecated for request accounting: the returned bucket is a snapshot,
        and mutating it does not reliably reach persistent storage. Use
        :meth:`_acquire`. Kept for inspecting bucket state.
        """
        key = self._get_bucket_key(url)
        bucket = self._storage.get_token_bucket(key)

        if bucket is None:
            # Create new bucket
            capacity = float(limit)
            refill_rate = capacity / window.total_seconds()
            bucket = TokenBucket(
                capacity=capacity,
                tokens=capacity,
                refill_rate=refill_rate,
            )
            self._storage.set_token_bucket(key, bucket)
        else:
            # Update refill rate if limit changed
            window_seconds = window.total_seconds()
            if window_seconds > 0:
                bucket.refill_rate = float(limit) / window_seconds
                bucket.capacity = float(limit)

        return bucket

    def _wait_for_token(self, bucket: TokenBucket, url: str) -> None:
        """
        Wait until a token is available in a caller-held bucket.

        Deprecated: operates on an in-memory snapshot, so the consumption is
        invisible to other workers. :meth:`_acquire` is the safe path.
        """
        wait_time = bucket.wait_time()
        if wait_time > 0:
            if self._raise_on_limit:
                raise RateLimitExceeded(
                    f"Rate limit exceeded for {url}. Wait {wait_time:.2f} seconds."
                )

            logger.info(
                f"Rate limit reached for {url}, waiting {wait_time:.2f} seconds"
            )
            time.sleep(wait_time)

        # Consume token
        bucket.refill()
        if not bucket.consume():
            # Should not happen after wait, but handle edge case
            time.sleep(0.1)
            bucket.refill()
            bucket.consume()

    def _update_from_response(self, response: requests.Response) -> None:
        """Update rate limit info from response headers."""
        detected = self._detector.detect_from_response(response)
        if not detected:
            return

        endpoint = self._get_endpoint_key(response.url)
        limit = detected.get("limit")
        remaining = detected.get("remaining")
        reset_time = detected.get("reset_time")
        window = detected.get("window")

        if limit and reset_time and window:
            rate_limit = RateLimit(
                endpoint=endpoint,
                limit=limit,
                remaining=remaining or limit,
                reset_time=reset_time,
                window=window,
                confidence=detected.get("confidence", "confirmed"),
            )
            self._storage.set_rate_limit(endpoint, rate_limit)

            # The server's own count is more authoritative than ours, so trust
            # it -- but write the corrected bucket back, or persistent backends
            # never see the correction.
            if remaining is not None:
                bucket = self._get_or_create_bucket(endpoint, limit, window)
                bucket.tokens = min(bucket.capacity, float(remaining))
                bucket.last_update = datetime.utcnow()
                self._storage.set_token_bucket(
                    self._get_bucket_key(endpoint), bucket
                )

            logger.debug(
                f"Rate limit updated for {endpoint}: {remaining}/{limit} remaining"
            )

    def _apply_default_limits(self, url: str) -> None:
        """Apply default limits if no rate limit info exists."""
        if not self._default_limits:
            return

        endpoint = self._get_endpoint_key(url)

        # Check if we already have rate limit info
        if self._storage.get_rate_limit(endpoint):
            return

        # Apply defaults
        if "requests_per_second" in self._default_limits:
            limit = self._default_limits["requests_per_second"]
            window = timedelta(seconds=1)
        elif "requests_per_minute" in self._default_limits:
            limit = self._default_limits["requests_per_minute"]
            window = timedelta(minutes=1)
        elif "requests_per_hour" in self._default_limits:
            limit = self._default_limits["requests_per_hour"]
            window = timedelta(hours=1)
        else:
            return

        # Create default rate limit
        rate_limit = RateLimit(
            endpoint=endpoint,
            limit=limit,
            remaining=limit,
            reset_time=datetime.utcnow() + window,
            window=window,
            confidence="configured",
        )
        self._storage.set_rate_limit(endpoint, rate_limit)

    def request(self, method: str, url: str, **kwargs) -> requests.Response:
        """
        Make a rate-limited HTTP request.

        Args:
            method: HTTP method (GET, POST, PUT, DELETE, PATCH)
            url: Request URL
            **kwargs: Additional arguments passed to requests.request()

        Returns:
            requests.Response object

        Raises:
            RateLimitExceeded: If raise_on_limit=True and limit is exceeded
        """
        return self._request(method, url, self._session, **kwargs)

    def _request(self, method: str, url: str, session, **kwargs) -> requests.Response:
        """
        Rate-limit and issue a request on a specific transport.

        Args:
            method: HTTP method.
            url: Request URL.
            session: Object with a ``request(method, url, **kwargs)`` method that
                performs the call. :meth:`wrap_session` passes the caller's own
                session here so its cookies, auth and adapters are preserved.
            **kwargs: Forwarded to the transport.
        """
        endpoint = self._get_endpoint_key(url)

        max_attempts = self._retry.max_attempts()
        for attempt in range(1, max_attempts + 1):
            # Defaults are (re)applied each attempt because a 429 may have
            # taught us a real limit in the meantime.
            self._apply_default_limits(url)

            rate_limit = self._storage.get_rate_limit(endpoint)
            if rate_limit and rate_limit.limit:
                self._acquire(endpoint, rate_limit.limit, rate_limit.window)

            response = session.request(method, url, **kwargs)
            self._update_from_response(response)

            if response.status_code not in self._retry.config.retry_on_status:
                return response

            if attempt >= max_attempts:
                # Out of attempts: hand the caller the server's own answer
                # rather than an exception it cannot inspect.
                logger.warning(
                    f"Giving up on {url} after {attempt} attempts "
                    f"(last status {response.status_code})"
                )
                return response

            wait_time = self._retry_delay(response, attempt)
            if self._raise_on_limit:
                raise RateLimitExceeded(
                    f"{url} returned {response.status_code}. "
                    f"Retry after {wait_time:.2f} seconds."
                )

            logger.warning(
                f"Received {response.status_code} for {url}, "
                f"waiting {wait_time:.2f} seconds before attempt {attempt + 1}"
            )
            time.sleep(wait_time)

        return response

    def _retry_delay(self, response: requests.Response, attempt: int) -> float:
        """
        Decide how long to wait before retrying a rejected request.

        The server's own ``Retry-After`` wins when present -- it knows when the
        window actually reopens -- capped at ``max_delay`` so a hostile or
        mistaken header cannot park the caller for hours. Otherwise fall back to
        the configured backoff.
        """
        retry_after = self._detector.retry_after_seconds(response.headers)
        if retry_after is not None:
            return min(retry_after, self._retry.config.max_delay)

        return self._retry.delay_for_attempt(attempt)

    def wrap_session(self, session: requests.Session) -> None:
        """
        Wrap an existing requests.Session with rate limiting.

        This modifies the session object in-place by wrapping its request method.
        The session stays the transport: its cookies, headers, auth, adapters,
        proxies and connection pool are all still used. Only the scheduling of
        the call is taken over.

        Args:
            session: requests.Session object to wrap
        """
        if getattr(session, "_smartratelimit_wrapped", False):
            return

        original_request = session.request

        transport = _OriginalRequestSession(original_request)

        def rate_limited_request(method, url, **kwargs):
            # Route through the limiter, but let it issue the call on this
            # session rather than on the limiter's own.
            return self._request(method, url, transport, **kwargs)

        session.request = rate_limited_request
        session._smartratelimit_wrapped = True

    def get_status(self, endpoint: str) -> Optional[RateLimitStatus]:
        """
        Get current rate limit status for an endpoint.

        Args:
            endpoint: Endpoint URL or domain

        Returns:
            RateLimitStatus object or None if no info available
        """
        # Normalize endpoint
        if not endpoint.startswith(("http://", "https://")):
            endpoint = f"https://{endpoint}"

        endpoint_key = self._get_endpoint_key(endpoint)
        rate_limit = self._storage.get_rate_limit(endpoint_key)

        if rate_limit:
            return rate_limit.to_status()

        return None

    def set_limit(
        self, endpoint: str, limit: int, window: str = "1h"
    ) -> None:
        """
        Manually set rate limit for an endpoint.

        Args:
            endpoint: Endpoint URL or domain
            limit: Maximum number of requests
            window: Time window (e.g., '1h', '1m', '30s', '1d')
        """
        # Normalize endpoint
        if not endpoint.startswith(("http://", "https://")):
            endpoint = f"https://{endpoint}"

        endpoint_key = self._get_endpoint_key(endpoint)

        # Parse window
        window_td = self._parse_window(window)

        rate_limit = RateLimit(
            endpoint=endpoint_key,
            limit=limit,
            remaining=limit,
            reset_time=datetime.utcnow() + window_td,
            window=window_td,
            confidence="configured",
        )

        self._storage.set_rate_limit(endpoint_key, rate_limit)

    def _parse_window(self, window: str) -> timedelta:
        """Parse window string to timedelta."""
        window = window.strip().lower()

        # Match patterns like "1h", "30m", "60s", "1d"
        match = None
        for pattern in ["d", "h", "m", "s"]:
            if window.endswith(pattern):
                try:
                    value = int(window[:-1])
                    if pattern == "d":
                        return timedelta(days=value)
                    elif pattern == "h":
                        return timedelta(hours=value)
                    elif pattern == "m":
                        return timedelta(minutes=value)
                    elif pattern == "s":
                        return timedelta(seconds=value)
                except ValueError:
                    pass

        # Default to 1 hour
        return timedelta(hours=1)

    def clear(self, endpoint: Optional[str] = None) -> None:
        """
        Clear stored rate limit data.

        Args:
            endpoint: Specific endpoint to clear, or None to clear all
        """
        if endpoint:
            if not endpoint.startswith(("http://", "https://")):
                endpoint = f"https://{endpoint}"
            endpoint_key = self._get_endpoint_key(endpoint)
            self._storage.clear(endpoint_key)
        else:
            self._storage.clear()

