"""Shared limiter logic.

:class:`~smartratelimit.core.RateLimiter` and
:class:`~smartratelimit.async_client.AsyncRateLimiter` differ only in how they
wait and how they issue a request. Everything else -- storage selection, scope
matching, detection bookkeeping, status, configuration -- lives here, so the two
cannot drift apart. They did once: header lookups were fixed in the sync limiter
and stayed case-sensitive in the async copy for three releases, and async
requests went out unpaced the whole time.
"""

import logging
from datetime import timedelta
from typing import Dict, List, Optional
from urllib.parse import urlparse

from smartratelimit._time import utcnow
from smartratelimit.detector import RateLimitDetector
from smartratelimit.models import (
    REQUESTS,
    LimitDimension,
    RateLimit,
    RateLimitStatus,
    TokenBucket,
)
from smartratelimit.providers import seed_limits
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


class StorageUnavailable(RateLimitExceeded):
    """
    Raised when shared storage cannot be reached and ``fail_closed`` is set.

    Subclasses :class:`RateLimitExceeded` so callers already handling "I was not
    allowed to send this" keep working.
    """

    pass


class LimiterBase:
    """State and bookkeeping common to the sync and async limiters."""

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
        fail_closed: bool = False,
        use_provider_profiles: bool = True,
        authenticated: bool = False,
    ):
        self._fail_closed = fail_closed
        self._use_provider_profiles = use_provider_profiles
        self._authenticated = authenticated
        self._seeded_hosts = set()
        self._storage = self._create_storage(storage)
        self._detector = RateLimitDetector(headers_map)
        self._default_limits = default_limits or {}
        self._raise_on_limit = raise_on_limit
        # Jitter by default: without it every client throttled by the same
        # window wakes up together and collides again on the retry.
        self._retry = RetryHandler(retry or RetryConfig(jitter=0.1))

    # ------------------------------------------------------------------
    # Storage
    # ------------------------------------------------------------------

    def _create_storage(self, storage) -> StorageBackend:
        """
        Build a storage backend from a connection string.

        A ready-made :class:`StorageBackend` is accepted too, for backends
        needing constructor options this string cannot express.
        """
        if isinstance(storage, StorageBackend):
            return storage

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
                return self._storage_fallback("SQLite", e)

        if storage.startswith("redis://") or storage.startswith("rediss://"):
            try:
                return RedisStorage(redis_url=storage, fail_closed=self._fail_closed)
            except ImportError as e:
                return self._storage_fallback("Redis", e, hint="pip install redis")
            except Exception as e:
                # RedisStorage already warns and keeps going when Redis is
                # merely unreachable, so reaching here means it could not be
                # constructed at all -- or fail_closed asked us to stop.
                return self._storage_fallback("Redis", e)

        raise ValueError(f"Unknown storage backend: {storage}")

    def _storage_fallback(self, name: str, error: Exception, hint: str = "") -> StorageBackend:
        """
        Decide what to do when the requested backend will not start.

        Falling back to memory keeps the job running, but it silently converts a
        shared limit into a per-process one -- every worker then gets the full
        quota. That is the right trade when the limit is advisory and the wrong
        one when it guards a paid quota, so ``fail_closed`` decides.
        """
        suffix = f" ({hint})" if hint else ""
        if self._fail_closed:
            raise StorageUnavailable(
                f"{name} storage is unavailable and fail_closed=True: {error}{suffix}"
            )

        logger.warning(
            "%s storage unavailable (%s)%s -- falling back to in-memory storage. "
            "Limits are now per-process, not shared. Pass fail_closed=True to "
            "raise instead.",
            name,
            error,
            suffix,
        )
        return MemoryStorage()

    # ------------------------------------------------------------------
    # Endpoint scopes
    # ------------------------------------------------------------------

    @staticmethod
    def _get_endpoint_key(url: str) -> str:
        """Extract the host-level endpoint key from a URL."""
        parsed = urlparse(url)
        return f"{parsed.scheme}://{parsed.netloc}"

    @classmethod
    def _normalize_scope(cls, endpoint: str) -> str:
        """
        Canonicalise a scope so the same endpoint always maps to one key.

        A scope is a host, optionally narrowed by a path prefix:
        ``https://api.example.com`` or ``https://api.example.com/search``.
        Query strings and fragments are dropped -- they identify a request, not
        a quota -- as is a trailing slash, so ``/search`` and ``/search/`` are
        one scope rather than two.
        """
        if not endpoint.startswith(("http://", "https://")):
            endpoint = f"https://{endpoint}"

        parsed = urlparse(endpoint)
        path = parsed.path.rstrip("/")
        return f"{parsed.scheme}://{parsed.netloc}{path}"

    @classmethod
    def _candidate_scopes(cls, url: str) -> List[str]:
        """
        Scopes that could govern ``url``, most specific first.

        ``https://api.example.com/v1/users/42`` yields the full path, then each
        parent path, then the bare host. The limiter uses the first of these
        that has a stored limit, so a narrow rule on ``/v1/users`` wins over the
        host-wide default without either having to know about the other.
        """
        parsed = urlparse(url)
        host = f"{parsed.scheme}://{parsed.netloc}"

        segments = [s for s in parsed.path.split("/") if s]
        candidates = []
        for i in range(len(segments), 0, -1):
            candidates.append(host + "/" + "/".join(segments[:i]))
        candidates.append(host)
        return candidates

    def _get_bucket_key(self, scope: str, limit_type: str = "default") -> str:
        """
        Token bucket key for one dimension of a scope.

        The ``requests`` dimension keeps the historical ``:default`` suffix, so
        buckets written by earlier versions are still found.
        """
        if limit_type == REQUESTS:
            limit_type = "default"
        return f"{scope}:{limit_type}"

    def _resolve_limit(self, url: str) -> Optional[RateLimit]:
        """Find the most specific stored limit governing ``url``."""
        return self._storage.get_rate_limit_for(self._candidate_scopes(url))

    @staticmethod
    def _normalize_cost(cost) -> Dict[str, float]:
        """
        Turn a caller's cost into a per-dimension mapping.

        ``None`` means one request. A bare number is that many requests. A dict
        is taken as given, and ``requests`` defaults to 1 within it -- a call
        costing 1500 tokens is still one request unless you say otherwise.
        """
        if cost is None:
            return {REQUESTS: 1.0}

        if isinstance(cost, (int, float)):
            return {REQUESTS: float(cost)}

        if not isinstance(cost, dict):
            raise TypeError(
                f"cost must be a number or a mapping of dimension to amount, "
                f"got {type(cost).__name__}"
            )

        normalized = {str(k): float(v) for k, v in cost.items()}
        normalized.setdefault(REQUESTS, 1.0)
        return normalized

    def _acquire_specs(self, rate_limit: RateLimit, cost) -> List[tuple]:
        """
        Build the ``(key, capacity, refill_rate, tokens)`` list for one request.

        Only dimensions this request actually spends are included, so a call
        with no token cost is not gated on the token budget.
        """
        costs = self._normalize_cost(cost)
        specs = []

        for dimension in rate_limit.all_dimensions():
            amount = costs.get(dimension.name, 0.0)
            if amount <= 0:
                continue

            window_seconds = dimension.window.total_seconds() if dimension.window else 0.0
            if dimension.limit <= 0 or window_seconds <= 0:
                continue

            capacity = float(dimension.limit)
            specs.append(
                (
                    self._get_bucket_key(rate_limit.endpoint, dimension.name),
                    capacity,
                    capacity / window_seconds,
                    amount,
                )
            )

        unknown = set(costs) - {d.name for d in rate_limit.all_dimensions()}
        for name in sorted(unknown):
            if costs[name] > 0:
                # Spending a dimension nothing meters is almost always a typo
                # or a limit you meant to configure; silently ignoring it would
                # look like the limiter working.
                logger.warning(
                    "Request to %s declares a cost for %r, which has no "
                    "configured limit — that cost is not being metered.",
                    rate_limit.endpoint,
                    name,
                )

        return specs

    # ------------------------------------------------------------------
    # Buckets
    # ------------------------------------------------------------------

    def _get_or_create_bucket(
        self, scope: str, limit: int, window: timedelta
    ) -> TokenBucket:
        """
        Get or create the token bucket for a scope.

        Deprecated for request accounting: the returned bucket is a snapshot,
        and mutating it does not reliably reach persistent storage. Use the
        limiter's ``_acquire``. Kept for inspecting bucket state.
        """
        key = self._get_bucket_key(scope)
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

    # ------------------------------------------------------------------
    # Detection bookkeeping
    # ------------------------------------------------------------------

    def _record_response(self, url: str, status_code: int, headers) -> None:
        """
        Update the stored quota from a response.

        Both limiters funnel here with a plain url/status/headers triple, so
        detection behaves identically for `requests`, httpx and aiohttp.
        """
        detections = self._detector.detect_all(url, status_code, headers)
        if not detections:
            return

        # Write to whichever scope is actually governing this URL, so a
        # path-scoped limit is refreshed rather than shadowed by a host entry.
        existing = self._resolve_limit(url)
        scope = existing.endpoint if existing else self._get_endpoint_key(url)

        for detected in detections:
            limit = detected.get("limit")
            remaining = detected.get("remaining")
            reset_time = detected.get("reset_time")
            window = detected.get("window")
            name = detected.get("dimension", REQUESTS)

            if not (limit and reset_time and window):
                continue

            current = existing.dimension(name) if existing else None
            # A registry seed is a placeholder and yields to real headers; an
            # explicitly configured limit does not.
            if current is not None and current.confidence == "configured":
                # You set this deliberately, most likely because the headers are
                # absent or wrong. Detection does not get to overrule that.
                logger.debug(
                    "Ignoring detected %s limit for %s: explicitly configured",
                    name,
                    scope,
                )
                continue

            dimension = LimitDimension(
                name=name,
                limit=limit,
                remaining=remaining if remaining is not None else limit,
                reset_time=reset_time,
                window=window,
                confidence=detected.get("confidence", "confirmed"),
            )

            base = existing or RateLimit(
                endpoint=scope,
                limit=limit,
                remaining=dimension.remaining,
                reset_time=reset_time,
                window=window,
                confidence=dimension.confidence,
            )
            existing = base.with_dimension(dimension)
            self._storage.set_rate_limit(scope, existing)

            # The server's own count is more authoritative than ours, so trust
            # it -- but write the corrected bucket back, or persistent backends
            # never see the correction.
            if remaining is not None:
                key = self._get_bucket_key(scope, name)
                bucket = self._storage.get_token_bucket(key)
                capacity = float(limit)
                refill_rate = capacity / window.total_seconds()
                if bucket is None:
                    bucket = TokenBucket(
                        capacity=capacity, tokens=capacity, refill_rate=refill_rate
                    )
                bucket.capacity = capacity
                bucket.refill_rate = refill_rate
                bucket.tokens = min(capacity, float(remaining))
                bucket.last_update = utcnow()
                self._storage.set_token_bucket(key, bucket)

            logger.debug(
                "Rate limit updated for %s [%s]: %s/%s remaining",
                scope,
                name,
                remaining,
                limit,
            )

    def _apply_provider_profile(self, url: str) -> None:
        """
        Seed documented limits for a known host, before its first response.

        Only fills scopes that have nothing stored, and marks what it writes
        ``confidence="registry"`` so the API's own headers replace it as soon as
        they arrive. The point is not to be authoritative — it is to keep the
        first request to a 60-per-hour endpoint from being sent blind.
        """
        if not self._use_provider_profiles:
            return

        parsed = urlparse(url)
        host = parsed.netloc.lower()
        if not host or host in self._seeded_hosts:
            return

        # One attempt per host per limiter: a miss is far more common than a
        # hit, and re-deciding it on every request is pure overhead.
        self._seeded_hosts.add(host)

        profile = seed_limits(host, authenticated=self._authenticated)
        if not profile:
            return

        base = f"{parsed.scheme}://{parsed.netloc}"
        for suffix, limits in profile.items():
            scope = self._normalize_scope(base + suffix)
            if self._storage.get_rate_limit(scope) is not None:
                continue

            for seed in limits:
                window_td = self._parse_window(seed.window)
                dimension = LimitDimension(
                    name=seed.dimension,
                    limit=seed.limit,
                    remaining=seed.limit,
                    reset_time=utcnow() + window_td,
                    window=window_td,
                    confidence="registry",
                )
                existing = self._storage.get_rate_limit(scope) or RateLimit(
                    endpoint=scope,
                    limit=seed.limit,
                    remaining=seed.limit,
                    reset_time=dimension.reset_time,
                    window=window_td,
                    confidence="registry",
                )
                self._storage.set_rate_limit(
                    scope, existing.with_dimension(dimension)
                )

            logger.debug("Seeded %s from the %s provider profile", scope, host)

    def _apply_default_limits(self, url: str) -> None:
        """Apply configured default limits if nothing governs this URL yet."""
        if not self._default_limits:
            return

        if self._resolve_limit(url) is not None:
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

        # Defaults are host-wide: they are a fallback for "this API told us
        # nothing", not a statement about one path.
        endpoint = self._get_endpoint_key(url)
        self._storage.set_rate_limit(
            endpoint,
            RateLimit(
                endpoint=endpoint,
                limit=limit,
                remaining=limit,
                reset_time=utcnow() + window,
                window=window,
                confidence="configured",
            ),
        )

    # ------------------------------------------------------------------
    # Public configuration and inspection
    # ------------------------------------------------------------------

    def get_status(self, endpoint: str) -> Optional[RateLimitStatus]:
        """
        Get current rate limit status for an endpoint.

        Args:
            endpoint: Endpoint URL, domain, or domain plus path prefix. A bare
                domain matches whichever scheme was actually stored, so an
                http-only API is not missed.

        Returns:
            RateLimitStatus object or None if no info available
        """
        for scope in self._status_candidates(endpoint):
            rate_limit = self._storage.get_rate_limit_for(self._candidate_scopes(scope))
            if rate_limit:
                return self._live_status(rate_limit)

        return None

    def _live_status(self, rate_limit: RateLimit) -> RateLimitStatus:
        """
        Report what the limiter will actually grant right now.

        The stored ``remaining`` is a snapshot: for a limit you configured it
        never moves at all, and for a detected one it ages from the moment the
        response arrived. The token bucket is what the next request is really
        checked against, so read the count from there when a bucket exists.
        """
        status = rate_limit.to_status()

        for name, dimension in status.dimensions.items():
            bucket = self._storage.get_token_bucket(
                self._get_bucket_key(rate_limit.endpoint, name)
            )
            if bucket is None:
                continue

            bucket.refill()
            dimension.remaining = int(bucket.tokens)
            if name == REQUESTS:
                status.remaining = dimension.remaining

        return status

    @classmethod
    def _status_candidates(cls, endpoint: str) -> List[str]:
        """
        Scheme variants to try for a lookup.

        Assuming https for a bare domain silently misses an http endpoint the
        limiter is actively pacing, so try both rather than guess.
        """
        if endpoint.startswith(("http://", "https://")):
            return [cls._normalize_scope(endpoint)]

        return [
            cls._normalize_scope(f"https://{endpoint}"),
            cls._normalize_scope(f"http://{endpoint}"),
        ]

    def list_endpoints(self) -> List[str]:
        """
        Every endpoint scope with a stored rate limit, most specific first.

        Args: none.

        Returns:
            Scope keys such as ``['https://api.example.com/search',
            'https://api.example.com']``.
        """
        return sorted(self._storage.list_endpoints(), key=lambda k: (-len(k), k))

    def set_limit(
        self,
        endpoint: str,
        limit: int,
        window: str = "1h",
        dimension: str = REQUESTS,
    ) -> None:
        """
        Manually set the rate limit for an endpoint scope.

        Args:
            endpoint: Endpoint URL or domain, optionally narrowed by a path
                prefix — ``'api.example.com'`` applies to the whole host, while
                ``'api.example.com/search'`` applies only to paths under
                ``/search`` and takes precedence there.
            limit: Maximum units of ``dimension`` allowed per window.
            window: Time window (e.g., '1h', '1m', '30s', '1d')
            dimension: What is being metered. Defaults to ``'requests'``. Call
                again with another name — ``'tokens'``, ``'requests_daily'`` —
                to add a second budget to the same scope; a request must then
                satisfy every one of them.

        Raises:
            ValueError: If ``window`` is not a whole number plus d/h/m/s.
        """
        scope = self._normalize_scope(endpoint)
        window_td = self._parse_window(window)

        new_dimension = LimitDimension(
            name=dimension,
            limit=limit,
            remaining=limit,
            reset_time=utcnow() + window_td,
            window=window_td,
            confidence="configured",
        )

        existing = self._storage.get_rate_limit(scope)
        if existing is None:
            existing = RateLimit(
                endpoint=scope,
                limit=limit,
                remaining=limit,
                reset_time=new_dimension.reset_time,
                window=window_td,
                confidence="configured",
            )

        self._storage.set_rate_limit(scope, existing.with_dimension(new_dimension))

    @staticmethod
    def _parse_window(window: str) -> timedelta:
        """
        Parse a window string like ``'30s'`` or ``'1h'`` into a timedelta.

        Raises rather than defaulting: silently reading ``'1.5h'`` as one hour
        gives you a limiter pacing against a window you never asked for, and
        nothing tells you.
        """
        text = window.strip().lower()
        units = {"d": "days", "h": "hours", "m": "minutes", "s": "seconds"}

        if len(text) > 1 and text[-1] in units:
            try:
                value = int(text[:-1])
            except ValueError:
                pass
            else:
                if value > 0:
                    return timedelta(**{units[text[-1]]: value})

        raise ValueError(
            f"Invalid window {window!r}: expected a positive whole number "
            f"followed by d, h, m or s (e.g. '30s', '15m', '1h', '1d')."
        )

    def clear(self, endpoint: Optional[str] = None) -> None:
        """
        Clear stored rate limit data.

        Args:
            endpoint: Specific endpoint scope to clear, or None to clear all.
        """
        if endpoint:
            self._storage.clear(self._normalize_scope(endpoint))
        else:
            self._storage.clear()

    # ------------------------------------------------------------------
    # Retry timing
    # ------------------------------------------------------------------

    def _retry_delay(self, headers, attempt: int) -> float:
        """
        Decide how long to wait before retrying a rejected request.

        The server's own ``Retry-After`` wins when present -- it knows when the
        window actually reopens -- capped at ``max_delay`` so a hostile or
        mistaken header cannot park the caller for hours. Otherwise fall back to
        the configured backoff.
        """
        retry_after = self._detector.retry_after_seconds(headers)
        if retry_after is not None:
            return min(retry_after, self._retry.config.max_delay)

        return self._retry.delay_for_attempt(attempt)
