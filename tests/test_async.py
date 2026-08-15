"""Tests for async rate limiter."""

import asyncio
from unittest.mock import AsyncMock, Mock, patch

import pytest

from smartratelimit import AsyncRateLimiter, RateLimitExceeded


@pytest.mark.asyncio
class TestAsyncRateLimiter:
    """Test AsyncRateLimiter functionality."""

    async def test_init(self):
        """Test async limiter initialization."""
        limiter = AsyncRateLimiter()
        assert limiter._storage is not None
        assert limiter._detector is not None

    async def test_context_manager(self):
        """Test async context manager."""
        async with AsyncRateLimiter() as limiter:
            assert limiter is not None
            assert isinstance(limiter, AsyncRateLimiter)

    async def test_get_status(self):
        """Test getting status from async limiter."""
        limiter = AsyncRateLimiter()
        limiter.set_limit("api.example.com", limit=100, window="1h")

        status = limiter.get_status("api.example.com")
        assert status is not None
        assert status.limit == 100

    async def test_set_limit(self):
        """Test setting limit from async limiter."""
        limiter = AsyncRateLimiter()
        limiter.set_limit("api.example.com", limit=50, window="30m")

        status = limiter.get_status("api.example.com")
        assert status is not None
        assert status.limit == 50

    async def test_clear(self):
        """Test clearing data from async limiter."""
        limiter = AsyncRateLimiter()
        limiter.set_limit("api.example.com", limit=100, window="1h")
        
        # Verify it was set
        status = limiter.get_status("api.example.com")
        assert status is not None
        assert status.limit == 100
        
        # Clear it
        limiter.clear("api.example.com")

        # Verify it's cleared
        status = limiter.get_status("api.example.com")
        assert status is None

    @pytest.mark.asyncio
    async def test_arequest_httpx(self):
        """Test httpx async request."""
        try:
            import httpx
        except ImportError:
            pytest.skip("httpx not installed")

        limiter = AsyncRateLimiter()
        limiter.set_limit("api.example.com", limit=100, window="1m")

        mock_response = Mock()
        mock_response.url = "https://api.example.com/test"
        mock_response.status_code = 200
        mock_response.headers = {}

        mock_client = AsyncMock()
        mock_client.request = AsyncMock(return_value=mock_response)

        response = await limiter.arequest_httpx(
            mock_client, "GET", "https://api.example.com/test"
        )

        assert response.status_code == 200
        mock_client.request.assert_called_once()

    @pytest.mark.asyncio
    async def test_arequest_httpx_with_rate_limit_headers(self):
        """Test httpx request with rate limit headers."""
        try:
            import httpx
        except ImportError:
            pytest.skip("httpx not installed")

        limiter = AsyncRateLimiter()

        mock_response = Mock()
        mock_response.url = "https://api.github.com/users"
        mock_response.status_code = 200
        mock_response.headers = {
            "X-RateLimit-Limit": "5000",
            "X-RateLimit-Remaining": "4999",
            "X-RateLimit-Reset": str(int(asyncio.get_event_loop().time()) + 3600),
        }

        mock_client = AsyncMock()
        mock_client.request = AsyncMock(return_value=mock_response)

        response = await limiter.arequest_httpx(
            mock_client, "GET", "https://api.github.com/users"
        )

        assert response.status_code == 200
        status = limiter.get_status("api.github.com")
        assert status is not None
        assert status.limit == 5000

    @pytest.mark.asyncio
    async def test_arequest_httpx_429_handling(self):
        """Test httpx request handling 429 responses."""
        try:
            import httpx
        except ImportError:
            pytest.skip("httpx not installed")

        limiter = AsyncRateLimiter()

        mock_response_429 = Mock()
        mock_response_429.url = "https://api.example.com/test"
        mock_response_429.status_code = 429
        mock_response_429.headers = {"Retry-After": "1"}

        mock_response_200 = Mock()
        mock_response_200.url = "https://api.example.com/test"
        mock_response_200.status_code = 200
        mock_response_200.headers = {}

        mock_client = AsyncMock()
        mock_client.request = AsyncMock(
            side_effect=[mock_response_429, mock_response_200]
        )

        with patch("asyncio.sleep", new_callable=AsyncMock):
            response = await limiter.arequest_httpx(
                mock_client, "GET", "https://api.example.com/test"
            )

        assert response.status_code == 200
        assert mock_client.request.call_count == 2

    @pytest.mark.asyncio
    async def test_arequest_aiohttp(self):
        """Test aiohttp async request."""
        try:
            import aiohttp
        except ImportError:
            pytest.skip("aiohttp not installed")

        limiter = AsyncRateLimiter()
        limiter.set_limit("api.example.com", limit=100, window="1m")

        mock_response = Mock()
        mock_response.url = "https://api.example.com/test"
        mock_response.status = 200
        mock_response.status_code = 200
        mock_response.headers = {}
        mock_response.read = AsyncMock(return_value=b'{"data": "test"}')

        # Create an async context manager mock
        class AsyncContextManager:
            async def __aenter__(self):
                return mock_response
            async def __aexit__(self, *args):
                return None

        mock_session = AsyncMock()
        mock_session.request = Mock(return_value=AsyncContextManager())

        response = await limiter.arequest_aiohttp(
            mock_session, "GET", "https://api.example.com/test"
        )

        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_wait_for_token(self):
        """Test async token waiting."""
        from smartratelimit.models import TokenBucket

        limiter = AsyncRateLimiter()
        bucket = TokenBucket(capacity=1.0, tokens=0.0, refill_rate=1.0)

        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await limiter._wait_for_token(bucket, "https://api.example.com/test")
            assert mock_sleep.called

    @pytest.mark.asyncio
    async def test_wait_for_token_raise_on_limit(self):
        """Test async token waiting with raise_on_limit."""
        from smartratelimit.models import TokenBucket

        limiter = AsyncRateLimiter(raise_on_limit=True)
        bucket = TokenBucket(capacity=1.0, tokens=0.0, refill_rate=1.0)

        with pytest.raises(RateLimitExceeded):
            await limiter._wait_for_token(bucket, "https://api.example.com/test")


    @pytest.mark.asyncio
    async def test_detects_lowercase_rate_limit_headers(self):
        """Headers arriving lowercase (HTTP/2, httpx) must still be detected.

        Regression: the detector matches header names literally, so converting
        the client's case-insensitive headers to a plain dict made every lookup
        miss and the async limiter silently stopped limiting.
        """
        limiter = AsyncRateLimiter()

        mock_response = Mock()
        mock_response.url = "https://api.example.com/users"
        mock_response.status_code = 200
        mock_response.headers = {
            "x-ratelimit-limit": "5000",
            "x-ratelimit-remaining": "4999",
            "x-ratelimit-reset": "3600",
        }

        mock_client = AsyncMock()
        mock_client.request = AsyncMock(return_value=mock_response)

        await limiter.arequest_httpx(mock_client, "GET", "https://api.example.com/users")

        status = limiter.get_status("api.example.com")
        assert status is not None, "lowercase rate limit headers were not detected"
        assert status.limit == 5000
        assert status.remaining == 4999

    @pytest.mark.asyncio
    async def test_detects_headers_from_real_httpx_headers_object(self):
        """The real httpx.Headers type (not a dict) must be understood."""
        httpx = pytest.importorskip("httpx")

        limiter = AsyncRateLimiter()

        mock_response = Mock()
        mock_response.url = "https://api.example.com/users"
        mock_response.status_code = 200
        mock_response.headers = httpx.Headers(
            {
                "X-RateLimit-Limit": "100",
                "X-RateLimit-Remaining": "42",
                "X-RateLimit-Reset": "60",
            }
        )

        mock_client = AsyncMock()
        mock_client.request = AsyncMock(return_value=mock_response)

        await limiter.arequest_httpx(mock_client, "GET", "https://api.example.com/users")

        status = limiter.get_status("api.example.com")
        assert status is not None
        assert status.limit == 100
        assert status.remaining == 42

    @pytest.mark.asyncio
    async def test_aiohttp_429_retry_returns_wrapped_response(self):
        """A retried 429 must come back in the same wrapper as any other response.

        Regression: the retry path returned the raw ClientResponse, which has no
        .status_code, so caller code that worked on the normal path broke as
        soon as a request was retried.
        """

        class FakeResponse:
            def __init__(self, status, headers, body):
                self.status = status
                self.headers = headers
                self.body = body
                self.url = "https://api.example.com/test"

            async def read(self):
                return self.body

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc_info):
                return False

        class FakeSession:
            def __init__(self, responses):
                self.responses = list(responses)
                self.call_count = 0

            def request(self, method, url, **kwargs):
                self.call_count += 1
                return self.responses.pop(0)

        limiter = AsyncRateLimiter()
        session = FakeSession(
            [
                FakeResponse(429, {"Retry-After": "1"}, b'{"error": "slow down"}'),
                FakeResponse(200, {}, b'{"ok": true}'),
            ]
        )

        with patch("asyncio.sleep", new_callable=AsyncMock):
            response = await limiter.arequest_aiohttp(
                session, "GET", "https://api.example.com/test"
            )

        assert session.call_count == 2
        assert response.status_code == 200
        assert response.status == 200
        assert await response.json() == {"ok": True}
