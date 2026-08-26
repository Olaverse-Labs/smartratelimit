# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.4.0] - 2026-08-26

### Fixed
- **Persistent backends did not rate limit at all.** `request()` consumed a token
  from an in-memory copy of the bucket, then re-read the bucket from storage
  before saving it — discarding the consumption. With `sqlite://` or `redis://`
  every request found a full bucket, so the limiter never throttled anything. It
  only appeared to work with `memory` storage, where the "copy" is the same
  object. Consumption now happens inside the storage backend.
- **Concurrent workers could overdraw a shared bucket.** Even with the write-back
  fixed, `get` / modify / `set` loses updates: two workers read 10 tokens, both
  write 9, and two requests cost one token. Storage backends now expose an
  atomic `acquire()` — `MemoryStorage` under its lock, `SQLiteStorage` inside a
  `BEGIN IMMEDIATE` transaction, `RedisStorage` as a server-side Lua script — so
  the "multi-process safe" claim holds for real. Covered by multi-process tests
  that fail if a single token is double-spent.
- **`Retry-After` as an HTTP-date was silently ignored.** RFC 9110 allows either
  a delay in seconds or a date, and real APIs send both. Parsing the date form
  raised `TypeError` comparing an aware datetime to a naive one, which was
  caught and treated as "no header", so the server's own timing was thrown away.
  Both forms now parse; a date already in the past yields 0 rather than a
  negative wait.
- **A 429 was retried at most once.** The retry path also only ran when
  `Retry-After` parsed as an integer, so a 429 with no header — or a date — was
  returned to the caller unretried. Requests now retry per `RetryConfig`,
  preferring the server's `Retry-After` and falling back to exponential backoff.
- **`wrap_session()` discarded the session it was given.** It replaced
  `session.request` with a call routed through the limiter's own private
  session, so the caller's headers, cookies, auth, adapters, proxies and
  connection pool were all dropped — the wrapped session was wrapped in name
  only. The session is now the transport; wrapping twice is a no-op.
- `MemoryStorage` cleanup raised `TypeError` on a stored limit with no reset
  time, which could surface during an unrelated read or write.
- README links pointed at a repository path that no longer exists.

### Added
- `StorageBackend.acquire(key, capacity, refill_rate, tokens=1.0)` — the atomic
  refill-and-consume operation the limiter now relies on. Custom backends must
  implement it.
- `RateLimitStatus.confidence` / `RateLimit.confidence`, one of `'confirmed'`
  (the API reported its window), `'estimated'` (a limit with no usable reset, so
  the window was assumed) or `'configured'` (set by you). A limit of 100 with no
  reset header could be per minute or per day; the assumption is now labelled
  instead of being reported as a reading. Surfaced by `smartratelimit status`
  and `smartratelimit probe`.
- `RateLimitDetector(default_window=...)` to control the window assumed for
  `'estimated'` detections.
- `RateLimiter(retry=RetryConfig(...))` and `AsyncRateLimiter(retry=...)` to
  configure request retries.
- `RetryConfig(jitter=...)` to spread retries out, so clients throttled by the
  same window do not all wake and collide again. Defaults to `0.0`; the
  limiters default to `0.1`.
- `RetryHandler.delay_for_attempt()` and `RetryHandler.max_attempts()`.

### Changed
- `RetryStrategy.NONE` now means a single attempt. Previously it still retried
  `max_retries` times with a zero delay, hammering an endpoint that had already
  said no.
- With `raise_on_limit=True`, a retryable server rejection (429/503/504) now
  raises `RateLimitExceeded` instead of returning the failed response, matching
  the flag's meaning of "raise rather than wait".
- Waiting for a token is bounded by `RateLimiter.MAX_WAIT_ATTEMPTS` (64).
  Reaching it raises `RateLimitExceeded` rather than waiting forever for an
  endpoint that other workers keep winning.
- SQLite connections now use WAL and a busy timeout, so concurrent limiters
  queue for the write lock instead of failing with "database is locked".
- `RedisStorage` stores bucket timestamps as Unix time (the Lua script cannot
  format ISO strings). Buckets written by earlier versions are still read.

## [0.3.2] - 2026-08-15

### Fixed
- `AsyncRateLimiter` failed to detect rate limits whenever response header names
  did not arrive in canonical casing. httpx lowercases header names, and HTTP/2
  requires lowercase on the wire, so async requests to most APIs (GitHub,
  Stripe, OpenAI, anything behind a modern proxy) were never rate limited at
  all. Header lookups are now case-insensitive, matching the sync limiter.
- `arequest_aiohttp` returned a raw `ClientResponse` after retrying a 429
  instead of the usual wrapper, so `response.status_code` raised
  `AttributeError` on the retry path only.

### Added
- Regression tests covering lowercase headers, real `httpx.Headers` objects, and
  the aiohttp 429 retry return type
- MkDocs documentation site published at
  https://olaverse-labs.github.io/smartratelimit/
- GitHub Actions workflow for PyPI publishing via trusted publishing

## [0.3.1] - 2026-08-15

### Changed
- License changed from MIT to Apache 2.0
- Documentation links corrected for PyPI compatibility

## [0.3.0] - 2024-11-15

### Added
- `AsyncRateLimiter` class for async/await support
- httpx integration (sync and async)
- aiohttp integration (async)
- Advanced retry logic with configurable strategies
- `RetryHandler` and `RetryConfig` classes
- Multiple retry strategies: exponential, linear, fixed, none
- `MetricsCollector` class for tracking rate limit usage
- Prometheus metrics export format
- JSON metrics export
- CLI tools (`smartratelimit` command)
- CLI commands: `status`, `clear`, `probe`, `list`
- Comprehensive examples for async usage
- Examples for retry logic and metrics

### Changed
- Updated package exports to include new async and retry classes
- Enhanced documentation with async examples
- Improved error handling in async operations

### Fixed
- Better async context management
- Improved response handling for aiohttp

## [0.2.0] - 2024-11-15

### Added
- SQLite storage backend for persistent rate limit state
- Redis storage backend for distributed/multi-process applications
- Multi-process support with shared state via Redis
- Performance benchmarks and testing utilities
- Comprehensive tests for SQLite and Redis backends
- Graceful fallback to memory storage on backend failures

### Changed
- Improved storage backend initialization with better error handling
- Enhanced documentation with SQLite and Redis examples

### Fixed
- Thread safety improvements in storage backends
- Better handling of storage backend failures

## [0.1.0] - 2024-11-15

### Added
- Core `RateLimiter` class with automatic rate limit detection
- Token bucket algorithm for rate limiting
- Header-based rate limit detection for GitHub, Stripe, Twitter, OpenAI, and standard APIs
- In-memory storage backend
- `requests` library integration
- Session wrapping functionality
- Rate limit status monitoring
- Manual rate limit configuration
- Custom header mapping support
- Exception raising option when limits are exceeded
- Comprehensive test suite
- Full documentation and examples

### Features
- Automatic detection of rate limits from HTTP response headers
- Support for standard `X-RateLimit-*` headers
- Support for `Retry-After` headers on 429 responses
- Thread-safe operations
- Zero-configuration usage
- Default limits fallback

