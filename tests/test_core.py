"""Tests for core RateLimiter class."""

from datetime import datetime, timedelta
from unittest.mock import Mock, patch

import pytest
import requests

from smartratelimit import RateLimiter, RateLimitExceeded
from smartratelimit.models import RateLimit
from smartratelimit.storage import MemoryStorage


class TestRateLimiter:
    """Test RateLimiter core functionality."""

    def test_init_default(self):
        """Test default initialization."""
        limiter = RateLimiter()
        assert limiter._storage is not None
        assert limiter._detector is not None

    def test_init_with_default_limits(self):
        """Test initialization with default limits."""
        limiter = RateLimiter(default_limits={"requests_per_minute": 60})
        assert limiter._default_limits == {"requests_per_minute": 60}

    def test_init_with_custom_headers(self):
        """Test initialization with custom header mapping."""
        headers_map = {"limit": "X-Custom-Limit"}
        limiter = RateLimiter(headers_map=headers_map)
        assert limiter._detector.custom_headers_map == headers_map

    def test_get_endpoint_key(self):
        """Test endpoint key extraction."""
        limiter = RateLimiter()
        key = limiter._get_endpoint_key("https://api.example.com/v1/users")
        assert key == "https://api.example.com"

    def test_set_limit(self):
        """Test manually setting rate limit."""
        limiter = RateLimiter()
        limiter.set_limit("api.example.com", limit=100, window="1h")

        status = limiter.get_status("api.example.com")
        assert status is not None
        assert status.limit == 100

    def test_get_status(self):
        """Test getting rate limit status."""
        limiter = RateLimiter()
        limiter.set_limit("api.example.com", limit=100, window="1h")

        status = limiter.get_status("api.example.com")
        assert status is not None
        assert status.limit == 100
        assert status.remaining == 100

    def test_get_status_nonexistent(self):
        """Test getting status for non-existent endpoint."""
        limiter = RateLimiter()
        status = limiter.get_status("api.example.com")
        assert status is None

    def test_clear(self):
        """Test clearing rate limit data."""
        limiter = RateLimiter()
        limiter.set_limit("api.example.com", limit=100, window="1h")
        limiter.clear("api.example.com")

        status = limiter.get_status("api.example.com")
        assert status is None

    def test_clear_all(self):
        """Test clearing all rate limit data."""
        limiter = RateLimiter()
        limiter.set_limit("api1.com", limit=100, window="1h")
        limiter.set_limit("api2.com", limit=200, window="1h")
        limiter.clear()

        assert limiter.get_status("api1.com") is None
        assert limiter.get_status("api2.com") is None

    @patch("smartratelimit.core.requests.Session.request")
    def test_request_with_rate_limit_headers(self, mock_request):
        """Test request with rate limit headers in response."""
        # Mock response with rate limit headers
        mock_response = Mock()
        mock_response.url = "https://api.github.com/users"
        mock_response.status_code = 200
        mock_response.headers = {
            "X-RateLimit-Limit": "5000",
            "X-RateLimit-Remaining": "4999",
            "X-RateLimit-Reset": str(int(datetime.utcnow().timestamp()) + 3600),
        }
        mock_request.return_value = mock_response

        limiter = RateLimiter()
        response = limiter.request("GET", "https://api.github.com/users")

        assert response.status_code == 200
        # Check that rate limit was stored
        status = limiter.get_status("api.github.com")
        assert status is not None
        assert status.limit == 5000

    @patch("smartratelimit.core.requests.Session.request")
    def test_request_with_default_limits(self, mock_request):
        """Test request with default limits applied."""
        mock_response = Mock()
        mock_response.url = "https://api.example.com/test"
        mock_response.status_code = 200
        mock_response.headers = {}
        mock_request.return_value = mock_response

        limiter = RateLimiter(default_limits={"requests_per_minute": 60})
        response = limiter.request("GET", "https://api.example.com/test")

        assert response.status_code == 200
        # Check that default limit was applied
        status = limiter.get_status("api.example.com")
        assert status is not None

    @patch("smartratelimit.core.requests.Session.request")
    @patch("smartratelimit.core.time.sleep")
    def test_request_waits_on_limit(self, mock_sleep, mock_request):
        """Test that request waits when rate limit is reached."""
        # Set up a rate limit
        limiter = RateLimiter()
        limiter.set_limit("api.example.com", limit=1, window="1m")

        # First request should succeed
        mock_response1 = Mock()
        mock_response1.url = "https://api.example.com/test"
        mock_response1.status_code = 200
        mock_response1.headers = {}

        # Second request should wait
        mock_response2 = Mock()
        mock_response2.url = "https://api.example.com/test"
        mock_response2.status_code = 200
        mock_response2.headers = {}

        mock_request.side_effect = [mock_response1, mock_response2]

        # Make first request
        limiter.request("GET", "https://api.example.com/test")

        # The second request has no token, so it waits for the window. Sleeping
        # is mocked out here, so no time actually passes and the token never
        # arrives -- the limiter gives up instead of spinning forever.
        with pytest.raises(RateLimitExceeded):
            limiter.request("GET", "https://api.example.com/test")

        # It waited for the real window, not an arbitrary delay.
        assert mock_sleep.called
        assert mock_sleep.call_args[0][0] == pytest.approx(60, abs=1)

    @patch("smartratelimit.core.requests.Session.request")
    def test_request_proceeds_after_waiting(self, mock_request):
        """A request that waits out the window is then allowed through."""
        limiter = RateLimiter()
        # Two per second: the second request waits ~0.5s, then proceeds.
        limiter.set_limit("api.example.com", limit=2, window="1s")

        def make_response():
            response = Mock()
            response.url = "https://api.example.com/test"
            response.status_code = 200
            response.headers = {}
            return response

        mock_request.side_effect = [make_response() for _ in range(3)]

        for _ in range(3):
            assert (
                limiter.request("GET", "https://api.example.com/test").status_code
                == 200
            )

        assert mock_request.call_count == 3

    @patch("smartratelimit.core.requests.Session.request")
    def test_request_raises_on_limit(self, mock_request):
        """Test that request raises exception when raise_on_limit=True."""
        limiter = RateLimiter(raise_on_limit=True)
        limiter.set_limit("api.example.com", limit=1, window="1m")

        mock_response = Mock()
        mock_response.url = "https://api.example.com/test"
        mock_response.status_code = 200
        mock_response.headers = {}
        mock_request.return_value = mock_response

        # First request should succeed
        limiter.request("GET", "https://api.example.com/test")

        # Second request should raise
        with pytest.raises(RateLimitExceeded):
            limiter.request("GET", "https://api.example.com/test")

    @patch("smartratelimit.core.requests.Session.request")
    def test_request_429_handling(self, mock_request):
        """Test handling of 429 responses."""
        # First response is 429
        mock_response_429 = Mock()
        mock_response_429.url = "https://api.example.com/test"
        mock_response_429.status_code = 429
        mock_response_429.headers = {"Retry-After": "1"}

        # Second response is success
        mock_response_200 = Mock()
        mock_response_200.url = "https://api.example.com/test"
        mock_response_200.status_code = 200
        mock_response_200.headers = {}

        mock_request.side_effect = [mock_response_429, mock_response_200]

        limiter = RateLimiter()
        with patch("smartratelimit.core.time.sleep"):
            response = limiter.request("GET", "https://api.example.com/test")

        # Should retry and get 200
        assert response.status_code == 200
        assert mock_request.call_count == 2

    def test_wrap_session(self):
        """Test wrapping existing session."""
        session = requests.Session()
        limiter = RateLimiter()

        limiter.wrap_session(session)

        # Verify session.request is wrapped
        assert hasattr(session, "request")
        assert callable(session.request)

    def test_parse_window(self):
        """Test window parsing."""
        limiter = RateLimiter()

        assert limiter._parse_window("1h") == timedelta(hours=1)
        assert limiter._parse_window("30m") == timedelta(minutes=30)
        assert limiter._parse_window("60s") == timedelta(seconds=60)
        assert limiter._parse_window("1d") == timedelta(days=1)

    def test_storage_fallback(self):
        """Test storage backend fallback."""
        # SQLite should work (or fallback to memory on error)
        limiter = RateLimiter(storage="sqlite:///test.db")
        # SQLite should work, but if it fails, it falls back to memory
        assert limiter._storage is not None

        # Redis should work if available, or fallback to memory
        try:
            limiter = RateLimiter(storage="redis://localhost:6379")
            # If Redis is available, it will use RedisStorage
            # If not, it falls back to MemoryStorage
            assert limiter._storage is not None
        except Exception:
            # If Redis fails, should fallback to memory
            pass

        # Invalid storage should raise
        with pytest.raises(ValueError):
            RateLimiter(storage="invalid://storage")

