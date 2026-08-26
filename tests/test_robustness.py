"""Tests for fail-closed storage, lookup normalisation, and window parsing."""

import logging
from unittest.mock import patch

import pytest

from smartratelimit import RateLimiter, StorageUnavailable
from smartratelimit.storage import RedisStorage

UNREACHABLE_REDIS = "redis://127.0.0.1:63999/0"


class TestFailClosed:
    """A limiter outage must be a choice, not a silent downgrade."""

    def test_unreachable_redis_warns_but_keeps_running(self, caplog):
        """Default is fail-open: the job keeps running, loudly."""
        with caplog.at_level(logging.WARNING):
            limiter = RateLimiter(storage=UNREACHABLE_REDIS)

        # redis-py connects lazily, so a dead Redis has to be detected rather
        # than waited for. The client is kept so it recovers on its own.
        assert isinstance(limiter._storage, RedisStorage)
        assert any("Cannot reach Redis" in r.getMessage() for r in caplog.records)

    def test_unreachable_redis_still_serves_requests(self, caplog):
        """Failing open means unpaced, not broken."""
        limiter = RateLimiter(storage=UNREACHABLE_REDIS)

        with caplog.at_level(logging.WARNING):
            allowed, wait = limiter._storage.acquire("k", 10.0, 1.0)

        assert allowed is True
        assert wait == 0.0

    def test_unreachable_redis_raises_when_fail_closed(self):
        with pytest.raises(StorageUnavailable):
            RateLimiter(storage=UNREACHABLE_REDIS, fail_closed=True)

    def test_acquire_fails_open_by_default(self, caplog):
        """A Redis that dies mid-run lets traffic through, with a warning."""
        storage = RedisStorage.__new__(RedisStorage)
        storage.key_prefix = "test:"
        storage.fail_closed = False
        storage._acquire_script = _boom

        with caplog.at_level(logging.WARNING):
            allowed, wait = storage.acquire("k", 10.0, 1.0)

        assert allowed is True
        assert wait == 0.0
        assert any("unpaced" in r.getMessage() for r in caplog.records)

    def test_acquire_fails_closed_when_asked(self):
        storage = RedisStorage.__new__(RedisStorage)
        storage.key_prefix = "test:"
        storage.fail_closed = True
        storage._acquire_script = _boom

        with pytest.raises(StorageUnavailable):
            storage.acquire("k", 10.0, 1.0)

    def test_storage_unavailable_is_a_rate_limit_error(self):
        """Callers already catching RateLimitExceeded keep working."""
        from smartratelimit import RateLimitExceeded

        assert issubclass(StorageUnavailable, RateLimitExceeded)


def _boom(*args, **kwargs):
    raise ConnectionError("redis is down")


class TestStatusLookup:
    """A bare domain must not silently assume https."""

    def test_finds_http_endpoint_from_bare_domain(self):
        limiter = RateLimiter()
        limiter.set_limit("http://api.example.com", limit=10, window="1m")

        status = limiter.get_status("api.example.com")
        assert status is not None
        assert status.limit == 10

    def test_finds_https_endpoint_from_bare_domain(self):
        limiter = RateLimiter()
        limiter.set_limit("https://api.example.com", limit=20, window="1m")

        assert limiter.get_status("api.example.com").limit == 20

    def test_explicit_scheme_is_respected(self):
        limiter = RateLimiter()
        limiter.set_limit("https://api.example.com", limit=20, window="1m")

        assert limiter.get_status("http://api.example.com") is None
        assert limiter.get_status("https://api.example.com").limit == 20

    def test_status_resolves_path_scopes(self):
        limiter = RateLimiter()
        limiter.set_limit("api.example.com/search", limit=10, window="1m")

        assert limiter.get_status("api.example.com/search/x").limit == 10

    def test_unknown_endpoint_is_none(self):
        assert RateLimiter().get_status("api.example.com") is None


class TestWindowParsing:
    """A window the caller mistyped must not become a silent one hour."""

    @pytest.mark.parametrize(
        "window,seconds",
        [("30s", 30), ("15m", 900), ("1h", 3600), ("2d", 172800), (" 1H ", 3600)],
    )
    def test_valid_windows(self, window, seconds):
        assert RateLimiter._parse_window(window).total_seconds() == seconds

    @pytest.mark.parametrize("window", ["1.5h", "h", "", "60", "1w", "abc", "-5m", "0s"])
    def test_invalid_windows_raise(self, window):
        with pytest.raises(ValueError, match="Invalid window"):
            RateLimiter._parse_window(window)

    def test_set_limit_rejects_a_bad_window(self):
        """Better a loud error at configuration time than wrong pacing forever."""
        with pytest.raises(ValueError):
            RateLimiter().set_limit("api.example.com", limit=10, window="1.5h")


class TestNoDeprecatedClock:
    """utcnow()/utcfromtimestamp() are removed in a future Python."""

    def test_library_does_not_call_deprecated_datetime_helpers(self):
        import pathlib

        offenders = []
        package = pathlib.Path(__file__).resolve().parent.parent / "smartratelimit"
        for path in package.glob("*.py"):
            if path.name == "_time.py":
                continue  # documents them in its docstring
            text = path.read_text()
            if "datetime.utcnow()" in text or "datetime.utcfromtimestamp(" in text:
                offenders.append(path.name)

        assert offenders == []


class TestLiveRemaining:
    """get_status() must report what the limiter will actually grant."""

    def test_remaining_tracks_consumption(self):
        from datetime import timedelta

        limiter = RateLimiter()
        limiter.set_limit("api.example.com", limit=10, window="1h")

        assert limiter.get_status("api.example.com").remaining == 10

        for _ in range(3):
            limiter._acquire("https://api.example.com", 10, timedelta(hours=1))

        # A stored snapshot would still say 10 here.
        assert limiter.get_status("api.example.com").remaining == 7

    def test_remaining_is_per_scope(self):
        from datetime import timedelta

        limiter = RateLimiter()
        limiter.set_limit("api.example.com", limit=10, window="1h")
        limiter.set_limit("api.example.com/search", limit=4, window="1h")

        for _ in range(3):
            limiter._acquire("https://api.example.com/search", 4, timedelta(hours=1))

        assert limiter.get_status("api.example.com/search").remaining == 1
        assert limiter.get_status("api.example.com").remaining == 10

    def test_status_without_a_bucket_uses_the_stored_value(self):
        limiter = RateLimiter()
        limiter.set_limit("api.example.com", limit=10, window="1h")

        assert limiter.get_status("api.example.com").remaining == 10
