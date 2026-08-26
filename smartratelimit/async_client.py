"""Async rate limiter for httpx and aiohttp."""

import asyncio
import logging
from datetime import timedelta
from typing import Dict, Optional

from smartratelimit._base import LimiterBase, RateLimitExceeded
from smartratelimit.retry import RetryConfig

logger = logging.getLogger(__name__)


class _AiohttpResponse:
    """
    An already-read aiohttp response that outlives its connection.

    aiohttp releases the connection when the ``async with`` block exits, which
    makes the body unreadable afterwards. Capturing it here lets the limiter
    retry inside the loop and still hand callers a response they can read.
    """

    def __init__(self, response, body):
        self._response = response
        self._body = body
        self.url = str(response.url)
        self.status_code = response.status
        self.status = response.status
        self.headers = response.headers

    async def read(self):
        return self._body

    async def json(self):
        import json

        return json.loads(self._body.decode())

    async def text(self):
        return self._body.decode()


class AsyncRateLimiter(LimiterBase):
    """
    Async rate limiter for httpx and aiohttp.

    Shares all of its bookkeeping with the synchronous
    :class:`~smartratelimit.core.RateLimiter` via :class:`LimiterBase`; only the
    waiting and the transport differ.

    Example:
        >>> import httpx
        >>> async with AsyncRateLimiter() as limiter:
        ...     async with httpx.AsyncClient() as client:
        ...         response = await limiter.arequest_httpx(client, 'GET', 'https://api.github.com/users')
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
        Initialize async rate limiter.

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

    async def __aenter__(self):
        """Async context manager entry."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        pass

    async def _acquire(self, scope: str, limit: int, window: timedelta) -> None:
        """
        Await a token for ``scope``, or raise.

        Mirrors :meth:`RateLimiter._acquire`: consumption happens atomically
        inside the storage backend, so concurrent tasks, processes and hosts
        share one honest count.
        """
        window_seconds = window.total_seconds() if window else 0.0
        if limit <= 0 or window_seconds <= 0:
            return

        capacity = float(limit)
        refill_rate = capacity / window_seconds
        key = self._get_bucket_key(scope)
        loop = asyncio.get_running_loop()

        for _ in range(self.MAX_WAIT_ATTEMPTS):
            # The storage backends are synchronous, and an atomic acquire can
            # block for as long as it takes another process to release the
            # SQLite write lock or Redis to run the script. Off the event loop
            # it goes, so one worker's contention does not stall every other
            # coroutine in the process.
            allowed, wait_time = await loop.run_in_executor(
                None, self._storage.acquire, key, capacity, refill_rate
            )
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
            await asyncio.sleep(max(wait_time, 0.001))

        raise RateLimitExceeded(
            f"Could not acquire a token for {scope} after "
            f"{self.MAX_WAIT_ATTEMPTS} attempts; the endpoint is saturated."
        )

    async def _pace(self, url: str) -> None:
        """Apply defaults, resolve the governing scope, and wait for a token."""
        self._apply_default_limits(url)

        rate_limit = self._resolve_limit(url)
        if rate_limit and rate_limit.limit:
            await self._acquire(rate_limit.endpoint, rate_limit.limit, rate_limit.window)

    def _should_give_up(self, status_code: int, attempt: int, max_attempts: int, url: str):
        """
        Decide whether to return this response or retry it.

        Returns True to stop, False to retry.
        """
        if status_code not in self._retry.config.retry_on_status:
            return True

        if attempt >= max_attempts:
            logger.warning(
                "Giving up on %s after %d attempts (last status %s)",
                url,
                attempt,
                status_code,
            )
            return True

        return False

    async def _wait_before_retry(self, url: str, status_code: int, headers, attempt: int) -> None:
        """Sleep for the server's requested delay, or the configured backoff."""
        wait_time = self._retry_delay(headers, attempt)
        if self._raise_on_limit:
            raise RateLimitExceeded(
                f"{url} returned {status_code}. Retry after {wait_time:.2f} seconds."
            )

        logger.warning(
            "Received %s for %s, waiting %.2f seconds before attempt %d",
            status_code,
            url,
            wait_time,
            attempt + 1,
        )
        await asyncio.sleep(wait_time)

    async def arequest_httpx(self, client, method: str, url: str, **kwargs):
        """
        Make a rate-limited async HTTP request using httpx.

        Args:
            client: httpx.AsyncClient instance
            method: HTTP method (GET, POST, PUT, DELETE, PATCH)
            url: Request URL
            **kwargs: Additional arguments passed to client.request()

        Returns:
            httpx.Response object
        """
        max_attempts = self._retry.max_attempts()

        for attempt in range(1, max_attempts + 1):
            await self._pace(url)

            response = await client.request(method, url, **kwargs)
            self._record_response(url, response.status_code, response.headers)

            if self._should_give_up(response.status_code, attempt, max_attempts, url):
                return response

            await self._wait_before_retry(
                url, response.status_code, response.headers, attempt
            )

        return response

    async def arequest_aiohttp(self, session, method: str, url: str, **kwargs):
        """
        Make a rate-limited async HTTP request using aiohttp.

        Args:
            session: aiohttp.ClientSession instance
            method: HTTP method (GET, POST, PUT, DELETE, PATCH)
            url: Request URL
            **kwargs: Additional arguments passed to session.request()

        Returns:
            A response wrapper exposing ``status_code``, ``status``, ``headers``
            and awaitable ``read()`` / ``json()`` / ``text()``.
        """
        max_attempts = self._retry.max_attempts()

        for attempt in range(1, max_attempts + 1):
            await self._pace(url)

            async with session.request(method, url, **kwargs) as response:
                # The body has to be read before the connection is released, so
                # every attempt -- retried or not -- comes back wrapped and
                # readable.
                body = await response.read()
                self._record_response(str(response.url), response.status, response.headers)
                wrapped = _AiohttpResponse(response, body)

            if self._should_give_up(wrapped.status_code, attempt, max_attempts, url):
                return wrapped

            await self._wait_before_retry(
                url, wrapped.status_code, wrapped.headers, attempt
            )

        return wrapped
