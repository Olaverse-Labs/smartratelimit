# Changelog

All notable changes to smartratelimit. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/), and the project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Current release: **v{{ smartratelimit_version }}**

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
