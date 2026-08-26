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
- **Per-path endpoint scopes.** `set_limit("api.example.com/search", limit=10,
  window="1m")` now scopes a limit to a path prefix, with its own token bucket.
  Resolution walks from the full path up to the bare host and uses the narrowest
  scope with a stored limit, so a host-wide default and a tight override
  coexist: exhausting `/search` no longer throttles `/users`. Previously every
  path on a host shared one bucket, forcing a choice between pacing everything
  at the strictest limit and blowing through it.
- `fail_closed=` on `RateLimiter`, `AsyncRateLimiter` and `RedisStorage`, plus
  `--fail-closed` on the CLI. An unreachable Redis still fails *open* by default
  (a limiter outage should not take your job down), but it now says so at
  WARNING level instead of swallowing the exception, and `fail_closed=True`
  raises `StorageUnavailable` rather than sending traffic unpaced — the right
  choice when the limit guards a paid quota.
- Redis is pinged at construction. `redis-py` connects lazily, so a dead Redis
  used to look healthy until the first request and then quietly stop limiting.
- `storage=` accepts a ready-made `StorageBackend` instance, for options the
  connection string cannot express (a custom Redis `key_prefix`, your own
  backend). The docs previously told you to assign to the private `_storage`.
- `StorageBackend.list_endpoints()` and `LimiterBase.list_endpoints()`. The CLI's
  `list` command now works instead of printing a note saying it cannot.
- `StorageBackend.get_rate_limit_for(candidates)` resolves a scope in one round
  trip; SQLite uses a single query and Redis a single pipeline.
- `RateLimitDetector.detect(url, status_code, headers)`, taking the three pieces
  rather than a response object, so `requests`, httpx and aiohttp reach
  identical logic.
- A `Tests` CI workflow: pytest across Python 3.8-3.13 with a real Redis service
  container, a build/`twine check`/version-consistency job, and a strict docs
  build on pull requests. Nothing ran the test suite in CI before, which is how
  a total limiting failure shipped and sat on PyPI. The workflow **fails if the
  Redis tests skip**, so a broken service container cannot masquerade as a green
  build.

### Changed
- Detected headers no longer overwrite a limit you set explicitly. A
  `confidence="configured"` entry stands, because you set it precisely when the
  headers were absent or wrong.
- `set_limit(window=...)` raises `ValueError` on anything that is not a positive
  whole number plus `d`/`h`/`m`/`s`. A mistyped `"1.5h"` silently becoming one
  hour paced you against a limit you never asked for, with nothing to tell you.
- `get_status()` with a bare domain now matches whichever scheme was stored.
  It assumed `https://` and returned `None` for an http endpoint the limiter was
  actively pacing.
- `smartratelimit status` with no endpoint lists every tracked endpoint instead
  of erroring, and prints `Confidence`.
- Sync and async limiters now share `LimiterBase`, so storage selection, scope
  matching, detection bookkeeping and configuration exist once. The async client
  was a near-copy, which is exactly how a header-casing fix landed on one path
  and left async requests unpaced for three releases.
- `datetime.utcnow()` and `datetime.utcfromtimestamp()` are gone (23 call sites),
  replaced by `smartratelimit._time` helpers with identical naive-UTC semantics.
  Both are deprecated from Python 3.12 and scheduled for removal.

- `clear(endpoint)` now removes the scopes nested under an endpoint and their
  buckets, rather than deleting a host's buckets while leaving its path limits
  behind. Matching is boundary-aware, so clearing `api.example.com` does not
  reach `api.example.com.evil.com`.
- `get_status().remaining` reads the live token bucket instead of a stored
  snapshot, which never moved for a configured limit and aged for a detected
  one.

### Removed
- `AsyncRateLimiter._wait_for_token()` and `_get_or_create_bucket()`-based
  consumption. They operated on a bucket snapshot, so the consumption never
  reached other workers. `_acquire()` is the supported path.

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
