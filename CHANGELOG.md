# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

