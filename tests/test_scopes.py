"""Tests for per-path endpoint scopes and scope resolution."""

import os
import tempfile
from unittest.mock import Mock, patch

import pytest
from requests.structures import CaseInsensitiveDict

from smartratelimit import RateLimiter, RateLimitExceeded
from smartratelimit.storage import MemoryStorage, RedisStorage, SQLiteStorage

REDIS_URL = "redis://localhost:6379/0"
REDIS_PREFIX = "smartratelimit-scope-test:"


def redis_available():
    try:
        import redis

        redis.from_url(REDIS_URL).ping()
        return True
    except Exception:
        return False


requires_redis = pytest.mark.skipif(not redis_available(), reason="Redis not available")


def make_response(status_code=200, headers=None, url="https://api.example.com/test"):
    response = Mock()
    response.url = url
    response.status_code = status_code
    response.headers = CaseInsensitiveDict(headers or {})
    return response


class TestScopeNormalization:
    """One endpoint must always map to one key."""

    @pytest.mark.parametrize(
        "given,expected",
        [
            ("api.example.com", "https://api.example.com"),
            ("https://api.example.com", "https://api.example.com"),
            ("http://api.example.com", "http://api.example.com"),
            ("api.example.com/search", "https://api.example.com/search"),
            ("api.example.com/search/", "https://api.example.com/search"),
            ("https://api.example.com/v1/users?page=2", "https://api.example.com/v1/users"),
            ("https://api.example.com/v1/users#frag", "https://api.example.com/v1/users"),
        ],
    )
    def test_normalizes(self, given, expected):
        assert RateLimiter._normalize_scope(given) == expected

    def test_candidates_are_most_specific_first(self):
        assert RateLimiter._candidate_scopes("https://api.example.com/v1/users/42") == [
            "https://api.example.com/v1/users/42",
            "https://api.example.com/v1/users",
            "https://api.example.com/v1",
            "https://api.example.com",
        ]

    def test_bare_host_candidates(self):
        assert RateLimiter._candidate_scopes("https://api.example.com") == [
            "https://api.example.com"
        ]


class TestScopeResolution:
    """The narrowest matching rule wins."""

    @pytest.fixture(params=["memory", "sqlite", pytest.param("redis", marks=requires_redis)])
    def limiter(self, request):
        if request.param == "memory":
            storage = MemoryStorage()
        elif request.param == "sqlite":
            storage = SQLiteStorage(os.path.join(tempfile.mkdtemp(), "scopes.db"))
        else:
            storage = RedisStorage(redis_url=REDIS_URL, key_prefix=REDIS_PREFIX)
            storage.clear()
            request.addfinalizer(storage.clear)

        return RateLimiter(storage=storage)

    def test_path_scope_beats_host_scope(self, limiter):
        limiter.set_limit("api.example.com", limit=100, window="1m")
        limiter.set_limit("api.example.com/search", limit=10, window="1m")

        assert limiter._resolve_limit("https://api.example.com/search").limit == 10
        assert limiter._resolve_limit("https://api.example.com/users").limit == 100

    def test_path_scope_covers_children(self, limiter):
        limiter.set_limit("api.example.com/v1", limit=50, window="1m")

        matched = limiter._resolve_limit("https://api.example.com/v1/users/42")
        assert matched.limit == 50
        assert matched.endpoint == "https://api.example.com/v1"

    def test_deepest_scope_wins(self, limiter):
        limiter.set_limit("api.example.com", limit=100, window="1m")
        limiter.set_limit("api.example.com/v1", limit=50, window="1m")
        limiter.set_limit("api.example.com/v1/search", limit=5, window="1m")

        assert limiter._resolve_limit("https://api.example.com/v1/search/x").limit == 5
        assert limiter._resolve_limit("https://api.example.com/v1/users").limit == 50
        assert limiter._resolve_limit("https://api.example.com/other").limit == 100

    def test_unmatched_host_has_no_limit(self, limiter):
        limiter.set_limit("api.example.com/search", limit=10, window="1m")

        assert limiter._resolve_limit("https://other.example.com/search") is None

    def test_scopes_have_independent_buckets(self, limiter):
        """The whole point: a tight path limit must not throttle the rest."""
        limiter.set_limit("api.example.com", limit=100, window="1h")
        limiter.set_limit("api.example.com/search", limit=2, window="1h")

        search = limiter._resolve_limit("https://api.example.com/search")
        users = limiter._resolve_limit("https://api.example.com/users")

        for _ in range(2):
            limiter._acquire(search.endpoint, search.limit, search.window)

        # /search is spent...
        strict = RateLimiter(storage=limiter._storage, raise_on_limit=True)
        with pytest.raises(RateLimitExceeded):
            strict._acquire(search.endpoint, search.limit, search.window)

        # ...but /users still has its own 100.
        for _ in range(10):
            limiter._acquire(users.endpoint, users.limit, users.window)

    def test_list_endpoints(self, limiter):
        limiter.set_limit("api.example.com", limit=100, window="1m")
        limiter.set_limit("api.example.com/search", limit=10, window="1m")

        listed = limiter.list_endpoints()
        assert set(listed) == {
            "https://api.example.com",
            "https://api.example.com/search",
        }
        # Most specific first
        assert listed[0] == "https://api.example.com/search"


class TestScopedRequests:
    """End-to-end through request()."""

    @patch("smartratelimit.core.requests.Session.request")
    def test_request_uses_the_matching_scope(self, mock_request):
        mock_request.return_value = make_response(url="https://api.example.com/search/x")

        limiter = RateLimiter(raise_on_limit=True)
        limiter.set_limit("api.example.com", limit=100, window="1h")
        limiter.set_limit("api.example.com/search", limit=1, window="1h")

        limiter.request("GET", "https://api.example.com/search/x")

        with pytest.raises(RateLimitExceeded):
            limiter.request("GET", "https://api.example.com/search/x")

        # A different path is unaffected.
        mock_request.return_value = make_response(url="https://api.example.com/users")
        assert limiter.request("GET", "https://api.example.com/users").status_code == 200


class TestConfiguredLimitsWin:
    """Detection must not overrule a limit you set deliberately."""

    @patch("smartratelimit.core.requests.Session.request")
    def test_detected_headers_do_not_override_configured(self, mock_request):
        mock_request.return_value = make_response(
            headers={"X-RateLimit-Limit": "5000", "X-RateLimit-Remaining": "4999"}
        )

        limiter = RateLimiter()
        limiter.set_limit("api.example.com", limit=10, window="1m")
        limiter.request("GET", "https://api.example.com/test")

        status = limiter.get_status("api.example.com")
        assert status.limit == 10
        assert status.confidence == "configured"

    @patch("smartratelimit.core.requests.Session.request")
    def test_detected_headers_still_refresh_detected_limits(self, mock_request):
        mock_request.return_value = make_response(
            headers={
                "X-RateLimit-Limit": "5000",
                "X-RateLimit-Remaining": "4999",
                "X-RateLimit-Reset": "60",
            }
        )

        limiter = RateLimiter()
        limiter.request("GET", "https://api.example.com/test")

        status = limiter.get_status("api.example.com")
        assert status.limit == 5000
        assert status.confidence == "confirmed"


class TestClearingScopes:
    """Clearing a host must take its path scopes with it."""

    @pytest.fixture(params=["memory", "sqlite", pytest.param("redis", marks=requires_redis)])
    def limiter(self, request):
        if request.param == "memory":
            storage = MemoryStorage()
        elif request.param == "sqlite":
            storage = SQLiteStorage(os.path.join(tempfile.mkdtemp(), "clear.db"))
        else:
            storage = RedisStorage(redis_url=REDIS_URL, key_prefix=REDIS_PREFIX)
            storage.clear()
            request.addfinalizer(storage.clear)

        return RateLimiter(storage=storage)

    def test_clearing_a_host_clears_its_paths(self, limiter):
        limiter.set_limit("api.example.com", limit=100, window="1m")
        limiter.set_limit("api.example.com/search", limit=10, window="1m")

        limiter.clear("api.example.com")

        assert limiter.list_endpoints() == []
        assert limiter._resolve_limit("https://api.example.com/search") is None

    def test_clearing_a_path_leaves_the_host(self, limiter):
        limiter.set_limit("api.example.com", limit=100, window="1m")
        limiter.set_limit("api.example.com/search", limit=10, window="1m")

        limiter.clear("api.example.com/search")

        assert limiter.list_endpoints() == ["https://api.example.com"]
        # /search now falls back to the host-wide rule
        assert limiter._resolve_limit("https://api.example.com/search").limit == 100

    def test_clearing_does_not_reach_a_lookalike_host(self, limiter):
        """A prefix match would let api.example.com clear api.example.com.evil.com."""
        limiter.set_limit("api.example.com", limit=100, window="1m")
        limiter.set_limit("api.example.com.evil.com", limit=1, window="1m")

        limiter.clear("api.example.com")

        assert limiter.list_endpoints() == ["https://api.example.com.evil.com"]

    def test_clearing_a_scope_clears_its_bucket(self, limiter):
        from datetime import timedelta

        limiter.set_limit("api.example.com/search", limit=2, window="1h")
        for _ in range(2):
            limiter._acquire("https://api.example.com/search", 2, timedelta(hours=1))

        limiter.clear("api.example.com/search")
        limiter.set_limit("api.example.com/search", limit=2, window="1h")

        # A stale bucket would leave this endpoint still exhausted.
        assert limiter.get_status("api.example.com/search").remaining == 2
