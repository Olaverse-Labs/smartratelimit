"""Core RateLimiter class."""

import logging
import time
from datetime import timedelta
from typing import Dict, Optional

import requests

from smartratelimit._base import (  # noqa: F401  (re-exported for callers)
    LimiterBase,
    RateLimitExceeded,
    StorageUnavailable,
)
from smartratelimit.retry import RetryConfig

logger = logging.getLogger(__name__)


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


class RateLimiter(LimiterBase):
    """
    Main rate limiter class that automatically manages API rate limits.

    Example:
        >>> limiter = RateLimiter()
        >>> response = limiter.request('GET', 'https://api.github.com/users')
        >>> print(response.status_code)
        200
    """

    def __init__(
        self,
        storage: str = "memory",
        default_limits: Optional[Dict[str, int]] = None,
        headers_map: Optional[Dict[str, str]] = None,
        raise_on_limit: bool = False,
        retry: Optional[RetryConfig] = None,
        fail_closed: bool = False,
    ):
        """
        Initialize rate limiter.

        Args:
            storage: Storage backend ('memory', 'sqlite:///path', 'redis://host:port'),
                or a ready-made StorageBackend instance
            default_limits: Default limits like {'requests_per_second': 10}
            headers_map: Custom header name mapping
            raise_on_limit: If True, raise exception instead of waiting
            retry: How to retry a request the server rejects with 429/503/504.
                Defaults to three attempts with jittered exponential backoff.
            fail_closed: If True, raise when shared storage is unreachable
                instead of silently falling back to per-process limits.
        """
        super().__init__(
            storage=storage,
            default_limits=default_limits,
            headers_map=headers_map,
            raise_on_limit=raise_on_limit,
            retry=retry,
            fail_closed=fail_closed,
        )
        self._session = requests.Session()

    def _acquire(self, scope: str, limit: int, window: timedelta) -> None:
        """
        Block until a token for ``scope`` has been consumed, or raise.

        Consumption happens inside the storage backend as one atomic step, so
        the count is correct across threads, processes and hosts. Reading a
        bucket here, deducting a token locally and writing it back would lose
        updates -- and with SQLite or Redis it would also lose the deduction
        entirely, since those backends hand back a fresh object each read.

        Args:
            scope: Endpoint scope key governing this request.
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
        key = self._get_bucket_key(scope)

        for _ in range(self.MAX_WAIT_ATTEMPTS):
            allowed, wait_time = self._storage.acquire(key, capacity, refill_rate)
            if allowed:
                return

            if self._raise_on_limit:
                raise RateLimitExceeded(
                    f"Rate limit exceeded for {scope}. Wait {wait_time:.2f} seconds."
                )

            if wait_time == float("inf"):
                raise RateLimitExceeded(
                    f"Rate limit for {scope} can never be satisfied: "
                    f"the bucket does not refill."
                )

            logger.info(
                "Rate limit reached for %s, waiting %.2f seconds", scope, wait_time
            )
            # Another worker may take the token we waited for, so loop rather
            # than assuming one wait is enough.
            time.sleep(max(wait_time, 0.001))

        raise RateLimitExceeded(
            f"Could not acquire a token for {scope} after "
            f"{self.MAX_WAIT_ATTEMPTS} attempts; the endpoint is saturated."
        )

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
        max_attempts = self._retry.max_attempts()

        for attempt in range(1, max_attempts + 1):
            # Defaults are (re)applied each attempt because a 429 may have
            # taught us a real limit in the meantime.
            self._apply_default_limits(url)

            rate_limit = self._resolve_limit(url)
            if rate_limit and rate_limit.limit:
                self._acquire(rate_limit.endpoint, rate_limit.limit, rate_limit.window)

            response = session.request(method, url, **kwargs)
            self._record_response(url, response.status_code, response.headers)

            if response.status_code not in self._retry.config.retry_on_status:
                return response

            if attempt >= max_attempts:
                # Out of attempts: hand the caller the server's own answer
                # rather than an exception it cannot inspect.
                logger.warning(
                    "Giving up on %s after %d attempts (last status %s)",
                    url,
                    attempt,
                    response.status_code,
                )
                return response

            wait_time = self._retry_delay(response.headers, attempt)
            if self._raise_on_limit:
                raise RateLimitExceeded(
                    f"{url} returned {response.status_code}. "
                    f"Retry after {wait_time:.2f} seconds."
                )

            logger.warning(
                "Received %s for %s, waiting %.2f seconds before attempt %d",
                response.status_code,
                url,
                wait_time,
                attempt + 1,
            )
            time.sleep(wait_time)

        return response

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

        transport = _OriginalRequestSession(session.request)

        def rate_limited_request(method, url, **kwargs):
            # Route through the limiter, but let it issue the call on this
            # session rather than on the limiter's own.
            return self._request(method, url, transport, **kwargs)

        session.request = rate_limited_request
        session._smartratelimit_wrapped = True
