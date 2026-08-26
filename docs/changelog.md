# Changelog

All notable changes to smartratelimit. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/), and the project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Current release: **v{{ smartratelimit_version }}**

## 0.4.0

### Fixed

- **Persistent backends did not rate limit at all.** `request()` consumed a
  token from an in-memory copy of the bucket, then re-read the bucket from
  storage before saving it — discarding the consumption. With `sqlite://` or
  `redis://` every request found a full bucket, so the limiter never throttled
  anything. It only appeared to work with `memory` storage, where the "copy" is
  the same object. Consumption now happens inside the storage backend.
- **Concurrent workers could overdraw a shared bucket.** Even with the
  write-back fixed, `get` / modify / `set` loses updates: two workers read 10
  tokens, both write 9, and two requests cost one token. Storage backends now
  expose an atomic `acquire()` — `MemoryStorage` under its lock,
  `SQLiteStorage` inside a `BEGIN IMMEDIATE` transaction, `RedisStorage` as a
  server-side Lua script — so the "multi-process safe" claim holds for real.
- **`Retry-After` as an HTTP-date was silently ignored.** Parsing the date form
  raised `TypeError` comparing an aware datetime to a naive one, which was
  caught and treated as "no header". Both forms now parse; a date already in the
  past yields 0 rather than a negative wait.
- **A 429 was retried at most once**, and only when `Retry-After` parsed as an
  integer. Requests now retry per `RetryConfig`, preferring the server's
  `Retry-After` and falling back to exponential backoff.
- **`wrap_session()` discarded the session it was given**, routing calls through
  the limiter's own private session and dropping the caller's headers, cookies,
  auth, adapters, proxies and connection pool. The session is now the transport.
- `MemoryStorage` cleanup raised `TypeError` on a stored limit with no reset
  time.

### Added

- `StorageBackend.acquire()`, the atomic refill-and-consume operation the
  limiter now relies on
- `RateLimitStatus.confidence` — `'confirmed'`, `'estimated'` or `'configured'`
  — so an assumed window is labelled rather than reported as a reading
- `RateLimitDetector(default_window=...)`
- `RateLimiter(retry=...)` and `AsyncRateLimiter(retry=...)`
- `RetryConfig(jitter=...)`, plus `RetryHandler.delay_for_attempt()` and
  `RetryHandler.max_attempts()`
- Multi-process tests for SQLite and Redis that fail if one token is
  double-spent

### Changed

- `RetryStrategy.NONE` now means a single attempt rather than `max_retries`
  zero-delay retries
- `raise_on_limit=True` raises on a retryable server rejection instead of
  returning the failed response
- Waiting for a token is bounded by `RateLimiter.MAX_WAIT_ATTEMPTS` (64)
- SQLite connections use WAL and a busy timeout
- `RedisStorage` stores bucket timestamps as Unix time; older buckets still read

## 0.3.2

### Fixed

- **`AsyncRateLimiter` was not rate limiting at all in most real deployments.**
  Header lookups were case-sensitive, but httpx lowercases header names and
  HTTP/2 requires lowercase on the wire — so limits went undetected against
  GitHub, Stripe, OpenAI and anything behind a modern proxy, and async requests
  went out unpaced. Lookups are now case-insensitive, matching the sync limiter.
- `arequest_aiohttp` returned a raw `ClientResponse` after retrying a 429
  instead of the usual wrapper, so `response.status_code` raised
  `AttributeError` on the retry path only.

### Added

- Regression tests for lowercase headers, real `httpx.Headers` objects, and the
  aiohttp 429 retry return type
- This documentation site
- PyPI publishing via GitHub Actions trusted publishing

## 0.3.1

### Changed

- Licence changed from MIT to Apache 2.0
- Documentation links corrected for PyPI rendering

## 0.3.0

### Added

- `AsyncRateLimiter` for async/await workloads
- httpx integration via `arequest_httpx()`
- aiohttp integration via `arequest_aiohttp()`
- Advanced retry logic: `RetryHandler`, `RetryConfig`, `RetryStrategy`
- Retry strategies: exponential, linear, fixed, none
- `MetricsCollector` for per-endpoint request and quota tracking
- Prometheus and JSON metrics export
- `smartratelimit` CLI with `status`, `clear`, `probe`, `list`
- Examples covering async, retry, and metrics

### Changed

- Package exports extended with the async, retry, and metrics classes
- Improved error handling in async operations

### Fixed

- Better async context management
- More reliable response handling for aiohttp

## 0.2.0

### Added

- SQLite storage backend for persistent rate limit state
- Redis storage backend for distributed and multi-process applications
- Shared state across processes via Redis
- Performance benchmarks and testing utilities
- Tests for the SQLite and Redis backends
- Graceful fallback to memory storage when a backend fails

### Changed

- Storage backend initialisation with clearer error handling

### Fixed

- Thread safety in the storage backends
- Handling of storage backend failures

## 0.1.0

### Added

- `RateLimiter` with automatic rate limit detection
- Token bucket pacing
- Header detection for GitHub, Stripe, Twitter, OpenAI, and standard `X-RateLimit-*` APIs
- `Retry-After` handling on 429 responses
- In-memory storage backend
- `requests` integration and session wrapping
- Rate limit status monitoring
- Manual limit configuration and custom header mapping
- `raise_on_limit` mode
- Thread-safe operation
