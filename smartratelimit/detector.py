"""Rate limit detection from HTTP headers."""

import re
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional, Tuple
from urllib.parse import urlparse

import requests
from requests.structures import CaseInsensitiveDict

from smartratelimit._time import utcfromtimestamp, utcnow

#: A rate limit read straight from the response: the server told us the limit
#: and when it resets, so the window is a fact.
CONFIDENCE_CONFIRMED = "confirmed"

#: A rate limit where the server gave a limit but no usable reset, so the
#: window below had to be assumed. Callers that care about correctness should
#: treat these as a hint and configure the real limit explicitly.
CONFIDENCE_ESTIMATED = "estimated"


class RateLimitDetector:
    """Detects rate limits from HTTP response headers."""

    # Common header patterns
    HEADER_PATTERNS = {
        # Standard patterns
        "limit": [
            "X-RateLimit-Limit",
            "X-RateLimit-Requests-Limit",  # RapidAPI pattern
            "RateLimit-Limit",
            "X-Rate-Limit-Limit",
        ],
        "remaining": [
            "X-RateLimit-Remaining",
            "X-RateLimit-Requests-Remaining",  # RapidAPI pattern
            "RateLimit-Remaining",
            "X-Rate-Limit-Remaining",
        ],
        "reset": [
            "X-RateLimit-Reset",
            "X-RateLimit-Requests-Reset",  # RapidAPI pattern
            "RateLimit-Reset",
            "X-Rate-Limit-Reset",
        ],
        "retry_after": [
            "Retry-After",
            "X-Retry-After",
        ],
    }

    # API-specific patterns
    API_PATTERNS = {
        "github.com": {
            "limit": "X-RateLimit-Limit",
            "remaining": "X-RateLimit-Remaining",
            "reset": "X-RateLimit-Reset",  # Unix timestamp
        },
        "api.stripe.com": {
            "limit": "Stripe-RateLimit-Limit",
            "remaining": "Stripe-RateLimit-Remaining",
            "reset": "Stripe-RateLimit-Reset",
        },
        "api.twitter.com": {
            "limit": "x-rate-limit-limit",
            "remaining": "x-rate-limit-remaining",
            "reset": "x-rate-limit-reset",  # Unix timestamp
        },
        "api.openai.com": {
            "limit": "x-ratelimit-limit-requests",
            "remaining": "x-ratelimit-remaining-requests",
            "reset": "x-ratelimit-reset-requests",
        },
        "api.anthropic.com": {
            "limit": "anthropic-ratelimit-requests-limit",
            "remaining": "anthropic-ratelimit-requests-remaining",
            "reset": "anthropic-ratelimit-requests-reset",
        },
    }

    #: Extra metered dimensions, beyond requests, for APIs that report them.
    #:
    #: For an LLM API the binding constraint is usually tokens per minute rather
    #: than requests per minute: a caller well inside its request budget still
    #: gets a 429 when the token budget is spent. Reading only the requests
    #: headers makes the limiter confidently pace against the wrong number.
    API_DIMENSIONS = {
        "api.openai.com": {
            "tokens": {
                "limit": "x-ratelimit-limit-tokens",
                "remaining": "x-ratelimit-remaining-tokens",
                "reset": "x-ratelimit-reset-tokens",
            },
        },
        "api.anthropic.com": {
            "tokens": {
                "limit": "anthropic-ratelimit-tokens-limit",
                "remaining": "anthropic-ratelimit-tokens-remaining",
                "reset": "anthropic-ratelimit-tokens-reset",
            },
            "input_tokens": {
                "limit": "anthropic-ratelimit-input-tokens-limit",
                "remaining": "anthropic-ratelimit-input-tokens-remaining",
                "reset": "anthropic-ratelimit-input-tokens-reset",
            },
            "output_tokens": {
                "limit": "anthropic-ratelimit-output-tokens-limit",
                "remaining": "anthropic-ratelimit-output-tokens-remaining",
                "reset": "anthropic-ratelimit-output-tokens-reset",
            },
        },
    }

    #: Header profiles for extra dimensions, tried on every host. Providers that
    #: follow the ``*-tokens`` convention are picked up without a named profile.
    GENERIC_DIMENSIONS = {
        "tokens": {
            "limit": "X-RateLimit-Limit-Tokens",
            "remaining": "X-RateLimit-Remaining-Tokens",
            "reset": "X-RateLimit-Reset-Tokens",
        },
    }

    #: Window assumed when a response advertises a limit but no reset time.
    #: There is no safe guess here -- a limit of 100 could be per minute or per
    #: day -- so the assumption is surfaced via ``confidence`` rather than
    #: passed off as detected fact.
    DEFAULT_WINDOW = timedelta(hours=1)

    def __init__(
        self,
        custom_headers_map: Optional[Dict[str, str]] = None,
        default_window: Optional[timedelta] = None,
    ):
        """
        Initialize detector with optional custom header mapping.

        Args:
            custom_headers_map: Custom mapping like {'limit': 'X-My-Limit', ...}
            default_window: Window to assume when a response advertises a limit
                but no reset time. Detections that fall back to it are marked
                ``confidence='estimated'``. Defaults to one hour.
        """
        self.custom_headers_map = custom_headers_map or {}
        self.default_window = default_window or self.DEFAULT_WINDOW

    def detect_from_response(
        self, response: requests.Response
    ) -> Optional[Dict[str, any]]:
        """
        Detect rate limit information from an HTTP response object.

        Returns:
            Dict with keys: limit, remaining, reset_time, window, confidence
            (``'confirmed'`` when the server supplied the window, ``'estimated'``
            when it had to be assumed) or None if no rate limit info found
        """
        return self.detect(
            response.url,
            getattr(response, "status_code", getattr(response, "status", 200)),
            response.headers,
        )

    def detect(
        self, url: str, status_code: int, headers
    ) -> Optional[Dict[str, any]]:
        """
        Detect rate limit information from a url, status and headers.

        Takes the three pieces rather than a response object so `requests`,
        httpx and aiohttp responses all reach identical logic -- the async
        client used to adapt its responses separately, and a header-casing fix
        landed on one path and not the other.

        Args:
            url: The request URL, used to pick API-specific header profiles.
            status_code: HTTP status; 429 unlocks the ``Retry-After`` fallback.
            headers: Response headers. Pass a case-insensitive mapping --
                httpx lowercases names and HTTP/2 requires lowercase on the
                wire, so a plain dict makes every lookup miss.

        Returns:
            Dict with keys: limit, remaining, reset_time, window, confidence,
            or None if no rate limit info found.
        """
        headers = self._as_case_insensitive(headers)

        # Get domain for API-specific patterns
        domain = urlparse(url).netloc.lower()

        # Try API-specific pattern first
        if domain in self.API_PATTERNS:
            pattern = self.API_PATTERNS[domain]
            result = self._extract_with_pattern(headers, pattern, domain)
            if result:
                return result

        # Try custom headers
        if self.custom_headers_map:
            result = self._extract_with_pattern(headers, self.custom_headers_map, domain)
            if result:
                return result

        # Try standard patterns
        for limit_header in self.HEADER_PATTERNS["limit"]:
            if limit_header in headers:
                pattern = {
                    "limit": limit_header,
                    "remaining": self._find_header(
                        headers, self.HEADER_PATTERNS["remaining"]
                    ),
                    "reset": self._find_header(
                        headers, self.HEADER_PATTERNS["reset"]
                    ),
                }
                result = self._extract_with_pattern(headers, pattern, domain)
                if result:
                    return result
                break

        # Try to extract from Retry-After on 429
        if status_code == 429:
            retry_after = self._find_header(headers, self.HEADER_PATTERNS["retry_after"])
            if retry_after:
                retry_seconds = self._parse_retry_after(headers[retry_after])
                if retry_seconds is not None:
                    return {
                        "limit": None,
                        "remaining": 0,
                        "reset_time": utcnow()
                        + timedelta(seconds=retry_seconds),
                        "window": timedelta(seconds=retry_seconds),
                        # The server named the wait explicitly.
                        "confidence": CONFIDENCE_CONFIRMED,
                    }

        return None

    def detect_all(self, url: str, status_code: int, headers) -> list:
        """
        Detect every metered dimension a response reports.

        Args:
            url: The request URL, used to pick API-specific header profiles.
            status_code: HTTP status; 429 unlocks the ``Retry-After`` fallback.
            headers: Response headers (any case-insensitive mapping).

        Returns:
            A list of detection dicts, each carrying a ``dimension`` key. The
            ``requests`` dimension, when found, comes first. Empty if nothing
            was detected.
        """
        headers = self._as_case_insensitive(headers)
        domain = urlparse(url).netloc.lower()

        detections = []

        primary = self.detect(url, status_code, headers)
        if primary:
            primary.setdefault("dimension", "requests")
            detections.append(primary)

        profiles = dict(self.GENERIC_DIMENSIONS)
        profiles.update(self.API_DIMENSIONS.get(domain, {}))

        for name, pattern in profiles.items():
            extra = self._extract_with_pattern(headers, pattern, domain)
            if extra:
                extra["dimension"] = name
                detections.append(extra)

        return detections

    @staticmethod
    def _as_case_insensitive(headers):
        """Wrap headers so lookups match regardless of the casing on the wire."""
        if headers is None:
            return CaseInsensitiveDict()
        if isinstance(headers, CaseInsensitiveDict):
            return headers
        return CaseInsensitiveDict(dict(headers))

    def _find_header(self, headers: Dict[str, str], candidates: list) -> Optional[str]:
        """Find first matching header from candidates."""
        for candidate in candidates:
            if candidate in headers:
                return candidate
        return None

    def _extract_with_pattern(
        self, headers: Dict[str, str], pattern: Dict[str, str], domain: str
    ) -> Optional[Dict[str, any]]:
        """Extract rate limit info using a specific header pattern."""
        limit_header = pattern.get("limit")
        remaining_header = pattern.get("remaining")
        reset_header = pattern.get("reset")

        if not limit_header or limit_header not in headers:
            return None

        try:
            limit = int(headers[limit_header])
        except (ValueError, TypeError):
            return None

        remaining = None
        if remaining_header and remaining_header in headers:
            try:
                remaining = int(headers[remaining_header])
            except (ValueError, TypeError):
                pass

        reset_time = None
        window = None

        if reset_header and reset_header in headers:
            reset_value = headers[reset_header]
            reset_time, window = self._parse_reset_time(reset_value, domain)

        # If we have limit but no remaining, assume we haven't hit it yet
        if remaining is None:
            remaining = limit

        if not limit:
            return None

        # The server gave a limit. Whether it also gave a usable window decides
        # how much this detection can be trusted -- a limit of 100 with no reset
        # header could be per minute or per day, and guessing wrong either
        # throttles the caller for nothing or lets them sail past the real limit.
        confidence = CONFIDENCE_CONFIRMED
        if reset_time is None:
            confidence = CONFIDENCE_ESTIMATED
            window = self.default_window
            reset_time = utcnow() + window
        elif window is None:
            window = reset_time - utcnow()
            if window.total_seconds() <= 0:
                confidence = CONFIDENCE_ESTIMATED
                window = self.default_window
                reset_time = utcnow() + window

        return {
            "limit": limit,
            "remaining": remaining,
            "reset_time": reset_time,
            "window": window,
            "confidence": confidence,
        }

    def _parse_reset_time(
        self, reset_value: str, domain: str
    ) -> Tuple[Optional[datetime], Optional[timedelta]]:
        """Parse reset time from header value."""
        try:
            # Try relative seconds first (if value is small, likely relative)
            # Unix timestamps are typically > 1000000000 (year 2001+)
            seconds = int(reset_value)
            if seconds < 86400:  # Less than 1 day, treat as relative seconds
                reset_time = utcnow() + timedelta(seconds=seconds)
                return reset_time, timedelta(seconds=seconds)
        except (ValueError, TypeError):
            pass

        try:
            # Try Unix timestamp (seconds) - for larger values
            timestamp = float(reset_value)
            if timestamp > 1000000000:  # Likely a Unix timestamp
                reset_time = utcfromtimestamp(timestamp)
                window = reset_time - utcnow()
                if window.total_seconds() > 0:  # Valid future time
                    return reset_time, window
        except (ValueError, TypeError, OSError):
            pass

        try:
            # Try ISO 8601 format
            reset_time = datetime.fromisoformat(reset_value.replace("Z", "+00:00"))
            window = reset_time - utcnow()
            if window.total_seconds() > 0:  # Valid future time
                return reset_time, window
        except (ValueError, TypeError):
            pass

        # Fallback: try as relative seconds
        try:
            seconds = int(reset_value)
            reset_time = utcnow() + timedelta(seconds=seconds)
            return reset_time, timedelta(seconds=seconds)
        except (ValueError, TypeError):
            pass

        return None, None

    def _parse_retry_after(self, retry_after: str) -> Optional[float]:
        """
        Parse a ``Retry-After`` value into seconds.

        RFC 9110 allows either a delay in seconds or an HTTP-date, and real APIs
        send both. A date that has already passed yields 0.0, not a negative
        wait.
        """
        if retry_after is None:
            return None

        value = retry_after.strip()

        try:
            return max(0.0, float(value))
        except (ValueError, TypeError):
            pass

        # HTTP-date format (RFC 9110). parsedate_to_datetime returns an aware
        # datetime, so compare against an aware "now" -- subtracting it from a
        # naive utcnow() raises TypeError and silently loses the header.
        try:
            from email.utils import parsedate_to_datetime

            retry_date = parsedate_to_datetime(value)
        except (ValueError, TypeError):
            return None

        if retry_date is None:
            return None

        if retry_date.tzinfo is None:
            retry_date = retry_date.replace(tzinfo=timezone.utc)

        delta = retry_date - datetime.now(timezone.utc)
        return max(0.0, delta.total_seconds())

    def retry_after_seconds(self, headers: Dict[str, str]) -> Optional[float]:
        """
        Read the server's requested wait from a response's headers.

        Args:
            headers: Response headers (any case-insensitive mapping).

        Returns:
            Seconds to wait, or None if no usable ``Retry-After`` is present.
        """
        header = self._find_header(headers, self.HEADER_PATTERNS["retry_after"])
        if header is None:
            return None
        return self._parse_retry_after(headers[header])

