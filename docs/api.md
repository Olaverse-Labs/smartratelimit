# API Reference

Everything importable from `smartratelimit`, plus the submodules you'll reach for.

```python
from smartratelimit import (
    RateLimiter,
    AsyncRateLimiter,
    RateLimitStatus,
    RateLimitExceeded,
    RetryConfig,
    RetryHandler,
    RetryStrategy,
    MetricsCollector,
)
```

---

## RateLimiter

`smartratelimit.RateLimiter`

Synchronous limiter built on `requests`.

### `RateLimiter(storage="memory", default_limits=None, headers_map=None, raise_on_limit=False)`

| Parameter | Type | Default | Description |
|---|---|---|---|
| `storage` | `str` | `"memory"` | `"memory"`, `"sqlite:///path.db"`, or `"redis://host:port/db"` |
| `default_limits` | `dict \| None` | `None` | Fallback limit when nothing is detected — one of `requests_per_second`, `requests_per_minute`, `requests_per_hour` |
| `headers_map` | `dict \| None` | `None` | Custom header names: keys `limit`, `remaining`, `reset` |
| `raise_on_limit` | `bool` | `False` | Raise `RateLimitExceeded` instead of waiting |
| `retry` | `RetryConfig \| None` | `None` | How to retry a 429/503/504. Defaults to three attempts with jittered exponential backoff |
| `fail_closed` | `bool` | `False` | Raise `StorageUnavailable` when shared storage is unreachable, instead of failing open and sending traffic unpaced |

Raises `ValueError` for an unrecognised storage string. `storage` also accepts a ready-made `StorageBackend` instance for options the connection string cannot express, such as a custom Redis `key_prefix`.

A SQLite backend that cannot open its file logs a warning and falls back to memory. An unreachable Redis is kept — and warned about at construction — so it recovers on its own when Redis returns. With `fail_closed=True` both raise instead.

If `default_limits` contains more than one key, the shortest window present wins (`second` → `minute` → `hour`) and the rest are ignored.

### `.request(method, url, **kwargs) -> requests.Response`

Make a paced request. `**kwargs` are passed through to `requests.request()`.

Waits (or raises, with `raise_on_limit=True`) when the endpoint's bucket is empty, then updates the stored quota from the response headers.

A 429, 503 or 504 is retried per the `retry` config. `Retry-After` decides the wait when present — seconds or HTTP-date, capped at `max_delay` — otherwise exponential backoff with jitter applies. When the attempts run out the last response is returned as-is. With `raise_on_limit=True` a retryable rejection raises `RateLimitExceeded` instead of being waited out.

### `.wrap_session(session) -> None`

Replace `session.request` with a paced version, in place. Returns `None`.

Your session stays the transport: its headers, cookies, auth, adapters, proxies and connection pool all continue to apply. Only the scheduling of the call is taken over. Wrapping the same session twice is a no-op.

### `.get_status(endpoint) -> RateLimitStatus | None`

Current stored status for an endpoint. Accepts a bare domain, a full URL, or a domain plus path prefix, and resolves to the narrowest scope governing it. A bare domain matches whichever scheme was actually stored, so an http-only API is not missed. Returns `None` when nothing has been detected or set.

### `.set_limit(endpoint, limit, window="1h") -> None`

Store a limit explicitly, without waiting to detect one.

`endpoint` is a domain or URL, optionally narrowed by a path prefix — `"api.example.com"` covers the host, `"api.example.com/search"` covers only paths under `/search` and takes precedence there. Each scope gets its own bucket.

`window` is a positive whole number plus a unit — `"30s"`, `"15m"`, `"1h"`, `"1d"`. Anything else, including a decimal like `"1.5h"`, raises `ValueError`: a mistyped window silently becoming one hour paces you against a limit you never asked for, with nothing to tell you.

What you set is marked `confidence="configured"`, and **detected headers will not overwrite it** — you set it because the headers were absent or wrong.

### `.list_endpoints() -> list[str]`

Every endpoint scope with a stored rate limit, most specific first.

### `.clear(endpoint=None) -> None`

Delete the stored quota and bucket for one endpoint, or for everything in the backend when `endpoint` is `None`.

---

## AsyncRateLimiter

`smartratelimit.AsyncRateLimiter`

Same constructor arguments as `RateLimiter`. Usable as an async context manager. See [Async](async.md).

### `await .arequest_httpx(client, method, url, **kwargs)`

Paced request through an `httpx.AsyncClient`. Returns the `httpx.Response`.

### `await .arequest_aiohttp(session, method, url, **kwargs)`

Paced request through an `aiohttp.ClientSession`. The body is read inside the response context and returned in a wrapper exposing `await .json()`, `await .text()`, `await .read()`, `.status`, `.status_code`, `.headers`, `.url`. Streaming is not supported on this path.

### `.get_status()`, `.set_limit()`, `.clear()`

Identical to the sync limiter, and **not** coroutines — call them without `await`.

---

## RateLimitStatus

`smartratelimit.RateLimitStatus` — a dataclass returned by `get_status()`.

| Attribute | Type | Description |
|---|---|---|
| `endpoint` | `str` | Normalised `scheme://host` |
| `limit` | `int` | Requests allowed per window |
| `remaining` | `int` | Requests left, as last reported or estimated |
| `reset_time` | `datetime \| None` | When the window resets (UTC, naive) |
| `window` | `timedelta \| None` | Length of the window |

| Property | Type | Description |
|---|---|---|
| `reset_in` | `float \| None` | Seconds until reset, floored at 0; `None` if `reset_time` is unset |
| `is_exceeded` | `bool` | `remaining <= 0` |
| `utilization` | `float` | `1 - remaining/limit`, in `0.0–1.0` (`1.0` when `limit` is 0) |

```python
status = limiter.get_status("api.github.com")
if status and status.utilization > 0.9:
    print(f"{status.remaining} left, resets in {status.reset_in:.0f}s")
```

---

## RateLimitExceeded

`smartratelimit.RateLimitExceeded` — subclass of `Exception`.

Raised by `request()` / `arequest_*()` when `raise_on_limit=True` and the bucket is empty. The message includes the wait that would have been taken.

---

## Retry

`smartratelimit.retry` — also re-exported from the package root.

### `RetryStrategy`

An `Enum`: `EXPONENTIAL`, `LINEAR`, `FIXED`, `NONE`.

### `RetryConfig(max_retries=3, strategy=RetryStrategy.EXPONENTIAL, base_delay=1.0, max_delay=60.0, backoff_factor=2.0, retry_on_status=None)`

`retry_on_status` defaults to `[429, 503, 504]`. Every computed delay is capped at `max_delay`.

### `RetryHandler(config=None)`

| Method | Description |
|---|---|
| `.should_retry(status_code, attempt) -> bool` | Whether this status warrants another attempt |
| `.retry_sync(func, *args, **kwargs)` | Call `func` with retries, sleeping between attempts |
| `await .retry_async(func, *args, **kwargs)` | Same for a coroutine function, awaiting between attempts |

Both retry on a retryable status code **and** on any raised exception. The final exception propagates; a final non-retryable-but-failed response is returned as-is. Details: [Retry Strategies](retry.md).

---

## MetricsCollector

`smartratelimit.metrics.MetricsCollector` — also exported from the package root.

| Method | Description |
|---|---|
| `.record_request(endpoint, status_code, rate_limit_status=None)` | Count one request; optionally append a status snapshot |
| `.get_metrics(endpoint=None) -> dict` | Counters for one endpoint (`{}` if unknown) or all |
| `.export_prometheus() -> str` | Prometheus text exposition format |
| `.export_json() -> str` | Indented JSON |
| `.reset(endpoint=None) -> None` | Drop metrics for one endpoint or all |

History is capped at the 100 most recent snapshots per endpoint. See [Metrics & Monitoring](metrics.md).

---

## Detector

`smartratelimit.detector.RateLimitDetector`

### `RateLimitDetector(custom_headers_map=None)`

### `.detect_from_response(response) -> dict | None`

Returns `{"limit", "remaining", "reset_time", "window"}` or `None`. Works on any object with `.headers`, `.url` and `.status_code`. Header list and resolution order: [Detection & Headers](detection.md).

Class attributes `HEADER_PATTERNS` and `API_PATTERNS` hold the recognised names and per-API profiles.

---

## Storage

`smartratelimit.storage`

### `StorageBackend`

Abstract base class. Implement `get_rate_limit`, `set_rate_limit`, `get_token_bucket`, `set_token_bucket`, `clear`.

### `MemoryStorage(cleanup_interval=3600)`

Thread-safe dicts; expired rate limits are swept no more than once per `cleanup_interval` seconds.

### `SQLiteStorage(db_path=":memory:")`

Creates `rate_limits` and `token_buckets` tables on construction. The parent directory must exist.

### `RedisStorage(redis_url="redis://localhost:6379/0", key_prefix="ratelimit:")`

Requires `redis`. Runtime read/write failures degrade quietly — reads return `None`, writes are dropped. Keys carry a TTL (window + 1h for limits, 24h for buckets). The raw client is available as `.redis_client`.

See [Storage Backends](storage.md).

---

## Models

`smartratelimit.models`

### `RateLimit`

Internal record — `endpoint`, `limit`, `remaining`, `reset_time`, `window`, `last_updated` — with `.to_status()` returning a `RateLimitStatus`.

### `TokenBucket`

`capacity`, `tokens`, `refill_rate` (tokens/second), `last_update`.

| Method | Description |
|---|---|
| `.refill(now=None)` | Add tokens for elapsed time, capped at `capacity` |
| `.consume(tokens=1.0, now=None) -> bool` | Refill then take tokens; `False` if not enough |
| `.wait_time(tokens=1.0, now=None) -> float` | Seconds until available; `inf` if `refill_rate <= 0` |
| `.reset()` | Back to full capacity |
