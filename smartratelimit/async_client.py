"""Async rate limiter for httpx and aiohttp."""

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Dict, Optional, Union
from urllib.parse import urlparse

from requests.structures import CaseInsensitiveDict

from smartratelimit.detector import RateLimitDetector
from smartratelimit.models import RateLimit, RateLimitStatus, TokenBucket
from smartratelimit.storage import StorageBackend

logger = logging.getLogger(__name__)


class AsyncRateLimiter:
    """
    Async rate limiter for httpx and aiohttp.

    Example:
        >>> import httpx
        >>> async with AsyncRateLimiter() as limiter:
        ...     async with httpx.AsyncClient() as client:
        ...         response = await limiter.arequest(client, 'GET', 'https://api.github.com/users')
    """

    def __init__(
        self,
        storage: str = "memory",
        default_limits: Optional[Dict[str, int]] = None,
        headers_map: Optional[Dict[str, str]] = None,
        raise_on_limit: bool = False,
    ):
        """
        Initialize async rate limiter.

        Args:
            storage: Storage backend ('memory', 'sqlite:///path', 'redis://host:port')
            default_limits: Default limits like {'requests_per_second': 10}
            headers_map: Custom header name mapping
            raise_on_limit: If True, raise exception instead of waiting
        """
        from smartratelimit.core import RateLimiter

        # Reuse the storage creation logic from sync RateLimiter
        sync_limiter = RateLimiter(
            storage=storage,
            default_limits=default_limits,
            headers_map=headers_map,
            raise_on_limit=raise_on_limit,
        )
        self._storage = sync_limiter._storage
        self._detector = sync_limiter._detector
        self._default_limits = sync_limiter._default_limits
        self._raise_on_limit = sync_limiter._raise_on_limit

    async def __aenter__(self):
        """Async context manager entry."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        pass

    @staticmethod
    def _get_endpoint_key(url: str) -> str:
        """Extract endpoint key from URL."""
        parsed = urlparse(url)
        return f"{parsed.scheme}://{parsed.netloc}"

    def _get_bucket_key(self, url: str, limit_type: str = "default") -> str:
        """Get token bucket key for URL."""
        endpoint = self._get_endpoint_key(url)
        return f"{endpoint}:{limit_type}"

    def _get_or_create_bucket(
        self, url: str, limit: int, window: timedelta
    ) -> TokenBucket:
        """Get or create token bucket for URL."""
        key = self._get_bucket_key(url)
        bucket = self._storage.get_token_bucket(key)

        if bucket is None:
            capacity = float(limit)
            refill_rate = capacity / window.total_seconds()
            bucket = TokenBucket(
                capacity=capacity,
                tokens=capacity,
                refill_rate=refill_rate,
            )
            self._storage.set_token_bucket(key, bucket)
        else:
            window_seconds = window.total_seconds()
            if window_seconds > 0:
                bucket.refill_rate = float(limit) / window_seconds
                bucket.capacity = float(limit)

        return bucket

    async def _wait_for_token(self, bucket: TokenBucket, url: str) -> None:
        """Wait until token is available (async)."""
        wait_time = bucket.wait_time()
        if wait_time > 0:
            if self._raise_on_limit:
                from smartratelimit.core import RateLimitExceeded

                raise RateLimitExceeded(
                    f"Rate limit exceeded for {url}. Wait {wait_time:.2f} seconds."
                )

            logger.info(
                f"Rate limit reached for {url}, waiting {wait_time:.2f} seconds"
            )
            await asyncio.sleep(wait_time)

        bucket.refill()
        if not bucket.consume():
            await asyncio.sleep(0.1)
            bucket.refill()
            bucket.consume()

    def _update_from_response(self, response) -> None:
        """Update rate limit info from response headers."""
        # Create a mock response-like object for detector
        class MockResponse:
            def __init__(self, response):
                self.url = str(response.url) if hasattr(response, "url") else response.url
                self.status_code = response.status_code if hasattr(response, "status_code") else getattr(response, "status", 200)
                # CaseInsensitiveDict, not dict: the detector matches header
                # names literally ("X-RateLimit-Limit"), while httpx lowercases
                # them and HTTP/2 requires the wire format to be lowercase. A
                # plain dict makes every lookup miss, so nothing is ever
                # detected and the async limiter silently stops limiting.
                self.headers = (
                    CaseInsensitiveDict(response.headers)
                    if hasattr(response, "headers")
                    else CaseInsensitiveDict()
                )

        mock_response = MockResponse(response)
        detected = self._detector.detect_from_response(mock_response)
        if not detected:
            return

        endpoint = self._get_endpoint_key(mock_response.url)
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
            )
            self._storage.set_rate_limit(endpoint, rate_limit)

            bucket = self._get_or_create_bucket(endpoint, limit, window)
            if remaining is not None:
                bucket.tokens = min(bucket.capacity, float(remaining))
                bucket.last_update = datetime.utcnow()

            logger.debug(
                f"Rate limit updated for {endpoint}: {remaining}/{limit} remaining"
            )

    def _apply_default_limits(self, url: str) -> None:
        """Apply default limits if no rate limit info exists."""
        if not self._default_limits:
            return

        endpoint = self._get_endpoint_key(url)
        if self._storage.get_rate_limit(endpoint):
            return

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

        rate_limit = RateLimit(
            endpoint=endpoint,
            limit=limit,
            remaining=limit,
            reset_time=datetime.utcnow() + window,
            window=window,
        )
        self._storage.set_rate_limit(endpoint, rate_limit)

    async def arequest_httpx(
        self, client, method: str, url: str, **kwargs
    ):
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
        endpoint = self._get_endpoint_key(url)
        self._apply_default_limits(url)

        rate_limit = self._storage.get_rate_limit(endpoint)

        if rate_limit:
            bucket = self._get_or_create_bucket(
                endpoint, rate_limit.limit, rate_limit.window
            )
            await self._wait_for_token(bucket, url)
        elif self._default_limits:
            self._apply_default_limits(url)
            rate_limit = self._storage.get_rate_limit(endpoint)
            if rate_limit:
                bucket = self._get_or_create_bucket(
                    endpoint, rate_limit.limit, rate_limit.window
                )
                await self._wait_for_token(bucket, url)

        response = await client.request(method, url, **kwargs)
        self._update_from_response(response)

        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")
            if retry_after:
                try:
                    wait_time = int(retry_after)
                    logger.warning(
                        f"Received 429 for {url}, waiting {wait_time} seconds"
                    )
                    if not self._raise_on_limit:
                        await asyncio.sleep(wait_time)
                        response = await client.request(method, url, **kwargs)
                        self._update_from_response(response)
                except (ValueError, TypeError):
                    pass

        if rate_limit:
            bucket = self._get_or_create_bucket(
                endpoint, rate_limit.limit, rate_limit.window
            )
            self._storage.set_token_bucket(self._get_bucket_key(endpoint), bucket)

        return response

    async def arequest_aiohttp(
        self, session, method: str, url: str, **kwargs
    ):
        """
        Make a rate-limited async HTTP request using aiohttp.

        Args:
            session: aiohttp.ClientSession instance
            method: HTTP method (GET, POST, PUT, DELETE, PATCH)
            url: Request URL
            **kwargs: Additional arguments passed to session.request()

        Returns:
            aiohttp.ClientResponse object
        """
        endpoint = self._get_endpoint_key(url)
        self._apply_default_limits(url)

        rate_limit = self._storage.get_rate_limit(endpoint)

        if rate_limit:
            bucket = self._get_or_create_bucket(
                endpoint, rate_limit.limit, rate_limit.window
            )
            await self._wait_for_token(bucket, url)
        elif self._default_limits:
            self._apply_default_limits(url)
            rate_limit = self._storage.get_rate_limit(endpoint)
            if rate_limit:
                bucket = self._get_or_create_bucket(
                    endpoint, rate_limit.limit, rate_limit.window
                )
                await self._wait_for_token(bucket, url)

        async with session.request(method, url, **kwargs) as response:
            # Read response body before updating
            body = await response.read()
            self._update_from_response(response)

            if response.status == 429:
                retry_after = response.headers.get("Retry-After")
                if retry_after:
                    try:
                        wait_time = int(retry_after)
                        logger.warning(
                            f"Received 429 for {url}, waiting {wait_time} seconds"
                        )
                        if not self._raise_on_limit:
                            await asyncio.sleep(wait_time)
                            async with session.request(method, url, **kwargs) as retry_response:
                                body = await retry_response.read()
                                self._update_from_response(retry_response)
                                # Fall through so the retried response is wrapped
                                # like every other one. Returning the raw
                                # ClientResponse here handed callers an object
                                # with no .status_code and a spent body, so code
                                # that worked on the normal path broke the moment
                                # a 429 was retried.
                                response = retry_response
                    except (ValueError, TypeError):
                        pass

            if rate_limit:
                bucket = self._get_or_create_bucket(
                    endpoint, rate_limit.limit, rate_limit.window
                )
                self._storage.set_token_bucket(
                    self._get_bucket_key(endpoint), bucket
                )

            # Create a response-like object that preserves the body
            class ResponseWrapper:
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

            return ResponseWrapper(response, body)

    def get_status(self, endpoint: str) -> Optional[RateLimitStatus]:
        """Get current rate limit status for an endpoint."""
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
        """Manually set rate limit for an endpoint."""
        # Normalize endpoint
        if not endpoint.startswith(("http://", "https://")):
            endpoint = f"https://{endpoint}"

        endpoint_key = self._get_endpoint_key(endpoint)
        
        # Parse window
        from smartratelimit.core import RateLimiter
        sync_limiter = RateLimiter()
        window_td = sync_limiter._parse_window(window)

        from datetime import datetime, timedelta
        rate_limit = RateLimit(
            endpoint=endpoint_key,
            limit=limit,
            remaining=limit,
            reset_time=datetime.utcnow() + window_td,
            window=window_td,
        )

        self._storage.set_rate_limit(endpoint_key, rate_limit)

    def clear(self, endpoint: Optional[str] = None) -> None:
        """Clear stored rate limit data."""
        if endpoint:
            # Normalize endpoint to match how it's stored
            if not endpoint.startswith(("http://", "https://")):
                endpoint = f"https://{endpoint}"
            endpoint_key = self._get_endpoint_key(endpoint)
            self._storage.clear(endpoint_key)
        else:
            self._storage.clear(None)

