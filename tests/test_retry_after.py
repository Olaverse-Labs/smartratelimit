"""Tests for Retry-After handling and detection confidence."""

import time
from datetime import datetime, timedelta
from email.utils import formatdate
from unittest.mock import Mock, patch

import pytest
from requests.structures import CaseInsensitiveDict

from smartratelimit import RateLimiter
from smartratelimit.detector import RateLimitDetector
from smartratelimit.retry import RetryConfig, RetryStrategy


def make_response(status_code=200, headers=None, url="https://api.example.com/test"):
    response = Mock()
    response.url = url
    response.status_code = status_code
    response.headers = CaseInsensitiveDict(headers or {})
    return response


class TestRetryAfterParsing:
    """RFC 9110 allows seconds or an HTTP-date; real APIs send both."""

    def test_parses_seconds(self):
        assert RateLimitDetector()._parse_retry_after("60") == 60.0

    def test_parses_http_date(self):
        """The date form used to be dropped entirely on a TypeError."""
        header = formatdate(time.time() + 120, usegmt=True)
        parsed = RateLimitDetector()._parse_retry_after(header)

        assert parsed is not None
        assert parsed == pytest.approx(120, abs=5)

    def test_past_http_date_is_zero_not_negative(self):
        header = formatdate(time.time() - 300, usegmt=True)
        assert RateLimitDetector()._parse_retry_after(header) == 0.0

    def test_fractional_seconds(self):
        assert RateLimitDetector()._parse_retry_after("1.5") == 1.5

    def test_garbage_is_ignored(self):
        assert RateLimitDetector()._parse_retry_after("soon") is None

    def test_retry_after_seconds_finds_the_header(self):
        detector = RateLimitDetector()
        headers = CaseInsensitiveDict({"retry-after": "45"})

        assert detector.retry_after_seconds(headers) == 45.0
        assert detector.retry_after_seconds(CaseInsensitiveDict()) is None


class TestDetectionConfidence:
    """A guessed window must not be presented as a detected one."""

    def test_reset_header_yields_confirmed(self):
        response = make_response(
            headers={
                "X-RateLimit-Limit": "100",
                "X-RateLimit-Remaining": "99",
                "X-RateLimit-Reset": "60",
            }
        )

        detected = RateLimitDetector().detect_from_response(response)
        assert detected["confidence"] == "confirmed"
        assert detected["window"].total_seconds() == pytest.approx(60, abs=2)

    def test_missing_reset_header_yields_estimated(self):
        """100 requests could be per minute or per day -- say so."""
        response = make_response(
            headers={"X-RateLimit-Limit": "100", "X-RateLimit-Remaining": "99"}
        )

        detected = RateLimitDetector().detect_from_response(response)
        assert detected["confidence"] == "estimated"
        assert detected["window"] == timedelta(hours=1)

    def test_assumed_window_is_configurable(self):
        detector = RateLimitDetector(default_window=timedelta(minutes=1))
        response = make_response(headers={"X-RateLimit-Limit": "100"})

        detected = detector.detect_from_response(response)
        assert detected["confidence"] == "estimated"
        assert detected["window"] == timedelta(minutes=1)

    @patch("smartratelimit.core.requests.Session.request")
    def test_confidence_reaches_the_public_status(self, mock_request):
        mock_request.return_value = make_response(
            headers={"X-RateLimit-Limit": "100", "X-RateLimit-Remaining": "99"}
        )

        limiter = RateLimiter()
        limiter.request("GET", "https://api.example.com/test")

        assert limiter.get_status("api.example.com").confidence == "estimated"

    def test_explicit_limits_are_marked_configured(self):
        limiter = RateLimiter()
        limiter.set_limit("api.example.com", limit=10, window="1m")

        assert limiter.get_status("api.example.com").confidence == "configured"


class TestRequestRetries:
    """A 429 should be retried properly, not once and only on an int header."""

    @patch("smartratelimit.core.time.sleep")
    @patch("smartratelimit.core.requests.Session.request")
    def test_retries_until_success(self, mock_request, mock_sleep):
        mock_request.side_effect = [
            make_response(429),
            make_response(429),
            make_response(200),
        ]

        limiter = RateLimiter()
        response = limiter.request("GET", "https://api.example.com/test")

        assert response.status_code == 200
        assert mock_request.call_count == 3

    @patch("smartratelimit.core.time.sleep")
    @patch("smartratelimit.core.requests.Session.request")
    def test_gives_up_after_max_retries(self, mock_request, mock_sleep):
        mock_request.return_value = make_response(429)

        limiter = RateLimiter(retry=RetryConfig(max_retries=2))
        response = limiter.request("GET", "https://api.example.com/test")

        # Caller gets the server's answer back rather than an exception.
        assert response.status_code == 429
        assert mock_request.call_count == 3

    @patch("smartratelimit.core.time.sleep")
    @patch("smartratelimit.core.requests.Session.request")
    def test_retry_after_beats_backoff(self, mock_request, mock_sleep):
        mock_request.side_effect = [
            make_response(429, {"Retry-After": "7"}),
            make_response(200),
        ]

        limiter = RateLimiter()
        limiter.request("GET", "https://api.example.com/test")

        mock_sleep.assert_called_once_with(7.0)

    @patch("smartratelimit.core.time.sleep")
    @patch("smartratelimit.core.requests.Session.request")
    def test_retry_after_http_date_is_honoured(self, mock_request, mock_sleep):
        mock_request.side_effect = [
            make_response(429, {"Retry-After": formatdate(time.time() + 30, usegmt=True)}),
            make_response(200),
        ]

        limiter = RateLimiter()
        limiter.request("GET", "https://api.example.com/test")

        assert mock_sleep.call_args[0][0] == pytest.approx(30, abs=5)

    @patch("smartratelimit.core.time.sleep")
    @patch("smartratelimit.core.requests.Session.request")
    def test_absurd_retry_after_is_capped(self, mock_request, mock_sleep):
        """A header asking for a week must not park the caller for a week."""
        mock_request.side_effect = [
            make_response(429, {"Retry-After": "604800"}),
            make_response(200),
        ]

        limiter = RateLimiter(retry=RetryConfig(max_delay=30.0))
        limiter.request("GET", "https://api.example.com/test")

        mock_sleep.assert_called_once_with(30.0)

    @patch("smartratelimit.core.time.sleep")
    @patch("smartratelimit.core.requests.Session.request")
    def test_backs_off_without_retry_after(self, mock_request, mock_sleep):
        """No header at all still retries -- the old path only handled ints."""
        mock_request.side_effect = [
            make_response(429),
            make_response(429),
            make_response(200),
        ]

        limiter = RateLimiter(
            retry=RetryConfig(
                strategy=RetryStrategy.EXPONENTIAL, base_delay=1.0, backoff_factor=2.0
            )
        )
        limiter.request("GET", "https://api.example.com/test")

        delays = [call[0][0] for call in mock_sleep.call_args_list]
        assert delays == [1.0, 2.0]

    @patch("smartratelimit.core.time.sleep")
    @patch("smartratelimit.core.requests.Session.request")
    def test_503_is_retried_too(self, mock_request, mock_sleep):
        mock_request.side_effect = [make_response(503), make_response(200)]

        limiter = RateLimiter()
        assert limiter.request("GET", "https://api.example.com/x").status_code == 200

    @patch("smartratelimit.core.requests.Session.request")
    def test_success_is_not_retried(self, mock_request):
        mock_request.return_value = make_response(200)

        limiter = RateLimiter()
        limiter.request("GET", "https://api.example.com/test")

        assert mock_request.call_count == 1


class TestJitter:
    """Jitter is opt-in so configured delays stay predictable."""

    def test_default_config_has_no_jitter(self):
        from smartratelimit.retry import RetryHandler

        handler = RetryHandler(RetryConfig(base_delay=1.0))
        assert handler._calculate_delay(2) == 2.0

    def test_jitter_spreads_delays(self):
        from smartratelimit.retry import RetryHandler

        handler = RetryHandler(RetryConfig(base_delay=10.0, jitter=0.5))
        delays = {handler._calculate_delay(1) for _ in range(50)}

        assert len(delays) > 1
        assert all(5.0 <= d <= 15.0 for d in delays)


class TestRetryStrategyNone:
    """RetryStrategy.NONE must mean no retry, not instant retries."""

    @patch("smartratelimit.core.time.sleep")
    @patch("smartratelimit.core.requests.Session.request")
    def test_none_strategy_does_not_retry(self, mock_request, mock_sleep):
        mock_request.return_value = make_response(429)

        limiter = RateLimiter(retry=RetryConfig(strategy=RetryStrategy.NONE))
        response = limiter.request("GET", "https://api.example.com/test")

        assert response.status_code == 429
        assert mock_request.call_count == 1
        assert mock_sleep.call_count == 0

    @patch("smartratelimit.core.time.sleep")
    @patch("smartratelimit.core.requests.Session.request")
    def test_zero_max_retries_does_not_retry(self, mock_request, mock_sleep):
        mock_request.return_value = make_response(429)

        limiter = RateLimiter(retry=RetryConfig(max_retries=0))
        limiter.request("GET", "https://api.example.com/test")

        assert mock_request.call_count == 1
