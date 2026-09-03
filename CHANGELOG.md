# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.5.1] - 2026-09-03

### Fixed
- **`simulate` under-reported wall-clock time when more than one budget was
  configured.** The wait before a request is `needed / refill_rate`, so
  refilling for exactly that long lands on the cost — but in floating point it
  can land a few ULPs below it, and `TokenBucket.consume`'s `tokens >= cost`
  check then refused and returned `False`. The simulator ignored that refusal
  and counted the request anyway, sending it for free.

  Over a long run this let the budget deliver more units than it ever supplied:
  4,410 RPM alongside 100k TPM at 2k tokens a request reported 18.1 minutes for
  1,000 requests, which would need 2,000,000 tokens against the 1,910,000 the
  budget hands out in that time. The correct answer is 19.00 minutes, where
  supply and spend balance exactly. The real limiter was never affected — it
  re-acquires after sleeping, so a wait a hair short simply sleeps again — but a
  simulator that disagrees with the thing it simulates is the one bug it cannot
  have. Charged directly after the wait now, and a conservation test asserts no
  run spends units the budget never supplied.

### Changed
- **The simulator's budget table names what each rate counts.** A token budget's
  ceiling is in *requests*, and rendering it as `50.0/min` next to a limit of
  100,000 tokens a minute invited exactly the wrong reading. The table now shows
  `RAW LIMIT` (the budget as configured, `100,000 tokens/min`), `COST/REQ`, and
  `EFFECTIVE CEILING` (`50 req/min`) as separate columns, so the arithmetic
  between them is visible. With `--keys` above 1 the report says which column is
  per key, since the ceiling is across all of them and the two would otherwise
  look inconsistent. The CLI and the docs playground render through the same
  column widths and unit logic.
- The playground's status line and output pane state their foreground colour.
  This theme's code background is dark in both schemes, so inheriting the body
  colour put near-black text on near-black in light mode.

## [0.5.0] - 2026-09-03

### Added
- **`smartratelimit simulate`** — model a workload against a set of rate limits
  without sending anything. Runs the library's own token buckets on a virtual
  clock, so an hour of traffic is modelled in milliseconds and the answer comes
  from the same code that paces real requests, rather than a separate model free
  to disagree with it.

  It reports the sustained ceiling each budget permits, which one actually held
  requests back, and how long the workload takes. The motivating case: 500 RPM
  alongside 100k TPM at 2k tokens a request looks comfortable and is in fact a
  50-requests-a-minute ceiling, ten times tighter than the request limit beside
  it — nineteen minutes for a thousand requests, with RPM never passing 11%.

  `--latency` turns worker count into a comparable ceiling, so you can tell
  whether the limit or your own concurrency is the constraint. `--keys` models
  spreading across API keys. `--limit NAME=COUNT/WINDOW` covers budgets beyond
  requests and tokens, and `--json` emits the same data for a machine.

  It deliberately does **not** predict 429s. It knows exactly when your limiter
  will hold a request back; the provider's behaviour additionally depends on
  burstiness, undocumented burst allowances, other clients on the same key, and
  token estimates being estimates. The command says so in its own output.
- `smartratelimit.simulate` (`Budget`, `simulate`, `SimulationResult`) for the
  same thing from Python.

- Browser playground in the docs: the simulator with sliders, running the
  library's own modules under Pyodide rather than a JavaScript mock. It loads
  three stdlib-only modules published alongside the page instead of installing
  from PyPI — `smartratelimit/__init__.py` eagerly imports `storage`, which
  imports `sqlite3`, and Pyodide unvendors `sqlite3` from the standard library,
  so the installed package cannot be imported in a browser at all. Tests assert
  the published copies stay byte-identical to the package.

### Fixed
- The test suite called `datetime.utcnow()` in nine files, emitting 271
  deprecation warnings — enough noise to bury a real one. One was a latent bug
  rather than a deprecation: `test_core` built a reset header with
  `datetime.utcnow().timestamp()`, and `.timestamp()` reads a naive datetime as
  *local* time, so on any machine that is not UTC the header landed hours in the
  past and the test quietly stopped exercising what it meant to. The guard test
  now covers `tests/` as well as the package, and scans its own file.

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
- **Multi-dimensional limits with per-request cost.** A scope can now meter more
  than requests. `set_limit(..., dimension="tokens")` adds a second budget, and
  `request(..., cost={"tokens": 1500})` says what a call spends; it is admitted
  only when every dimension it touches can pay. This is the constraint that
  actually binds on LLM APIs — a caller well inside its requests-per-minute
  allowance still gets a 429 once tokens-per-minute is spent, and pacing against
  requests alone paced against the wrong number.

  The charge is **atomic across every bucket**: `StorageBackend.acquire_many()`
  debits all of them or none. Charging in turn would let a request spend its
  request allowance and then be refused for tokens, draining the wrong budget on
  every rejection. Implemented as one lock for memory, one `BEGIN IMMEDIATE`
  transaction for SQLite, and one multi-key Lua script for Redis. A dimension a
  request does not spend never gates it.

  OpenAI and Anthropic token headers are detected automatically, as is the
  generic `X-RateLimit-*-Tokens` convention. The detector previously read only
  `x-ratelimit-limit-requests` from OpenAI and ignored the token headers
  entirely.
- **Provider profiles** for limits detection cannot reach in time.
  `RateLimiter()` seeds documented limits for known hosts before their first
  response; `authenticated=True` selects the credentialed numbers, since no
  response tells you which side you are on before you send one. GitHub's
  unauthenticated 60-per-hour is the motivating case — low enough that learning
  it from the first response has already cost a meaningful slice of the budget.

  Seeds carry `confidence="registry"` and are replaced as soon as the API
  reports its own numbers; an explicitly configured limit still outranks both.
  Disable with `use_provider_profiles=False`.

  **The built-in table is deliberately tiny and a test enforces that.** Most
  providers meter per account tier, so a baked-in number is a guess about the
  caller's account — and a wrong baked-in limit is worse than none. OpenAI,
  Anthropic and Stripe are excluded for exactly this reason; their limits come
  from headers. `register_provider()` covers your own services, which is the
  case the mechanism is really for.
- `LimitDimension`, and `RateLimitStatus.dimensions` exposing every metered
  budget. `smartratelimit status`, `list` and `probe` show them.
- `RateLimitDetector.detect_all()`, returning every dimension a response reports.
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
- `field(default_factory=datetime.utcnow)` in `models.py` survived the
  deprecation sweep, which matched only call sites with parentheses. The guard
  test now matches the bare reference too.
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

