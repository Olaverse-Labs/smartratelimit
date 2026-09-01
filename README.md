# smartratelimit

[![Python Version](https://img.shields.io/badge/python-3.8+-blue.svg)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](https://github.com/Olaverse-Labs/smartratelimit/blob/main/LICENSE)
[![PyPI version](https://badge.fury.io/py/smartratelimit.svg)](https://badge.fury.io/py/smartratelimit)
[![PyPI downloads](https://img.shields.io/pypi/dm/smartratelimit.svg)](https://pypi.org/project/smartratelimit/)
[![Docs](https://img.shields.io/badge/docs-olaverse--labs.github.io-7C5CFF.svg)](https://olaverse-labs.github.io/smartratelimit/)

A Python library that automatically manages API rate limits, preventing 429 errors and optimizing API usage without requiring developers to manually track or implement rate limiting logic.

## Features

- 🚀 **Automatic Detection**: Automatically detects rate limits from HTTP response headers
- 🔄 **Zero Configuration**: Works out of the box with most APIs
- 💾 **Persistent State**: Supports in-memory, SQLite, and Redis storage
- 🔀 **Multi-Process Safe**: SQLite and Redis backends consume tokens atomically, so workers on one box or many share one honest count
- 🎚️ **Per-Path Limits**: Scope a limit to a path prefix — `/search` at 10/min alongside the host's 100/min — with the narrowest rule winning
- 🧮 **Multi-Dimensional Limits**: Meter tokens per minute alongside requests per minute, charged atomically together — the budget that actually binds on LLM APIs
- 📇 **Provider Profiles**: Documented limits for APIs that don't advertise them, so the first request isn't sent blind
- 🎯 **Smart Waiting**: Automatically waits when limits are reached
- 📊 **Status Monitoring**: Check current rate limit status anytime
- 🔌 **Easy Integration**: Works with `requests`, `httpx`, and `aiohttp`
- 🔄 **Advanced Retry**: Honours `Retry-After` (seconds or HTTP-date), then exponential backoff with jitter
- 📊 **Metrics**: Built-in metrics collection and Prometheus export
- 🛠️ **CLI Tools**: Command-line interface for monitoring and management

## Installation

```bash
pip install smartratelimit
```

For async support:
```bash
pip install smartratelimit[httpx]  # For httpx support
pip install smartratelimit[aiohttp]  # For aiohttp support
pip install smartratelimit[all]  # For all optional dependencies
```

## Quick Start

### Basic Usage

```python
from smartratelimit import RateLimiter

# Create a rate limiter (auto-detects limits from headers)
limiter = RateLimiter()

# Make requests - rate limiting is automatic!
response = limiter.request('GET', 'https://api.github.com/users/octocat')
print(response.json())
```

### With SQLite Persistence

```python
# Persist rate limits across application restarts
limiter = RateLimiter(storage='sqlite:///rate_limits.db')

response = limiter.request('GET', 'https://api.github.com/users')
# Rate limit state is saved to database
```

### With Redis (Multi-Process)

```python
# Share rate limits across multiple processes/workers
limiter = RateLimiter(storage='redis://localhost:6379/0')

# Works with Gunicorn, Celery, etc.
response = limiter.request('GET', 'https://api.github.com/users')
```

#### How the shared count stays honest

Sharing a *store* between workers is not the same as sharing a *limit*. If each
worker reads the bucket, deducts a token locally and writes it back, two workers
that read at the same moment both see 10 tokens, both write 9, and two requests
cost one token.

So the token is consumed inside the store, as one indivisible step:

| Backend | Mechanism | Safe across |
| --- | --- | --- |
| `memory` | Consumption under the storage lock | Threads in one process |
| `sqlite://` | `BEGIN IMMEDIATE` write transaction | Processes on one machine |
| `redis://` | Server-side Lua script, Redis server clock | Processes on many machines |

The refill-check-consume cycle never crosses a process boundary mid-flight, so a
bucket cannot be overdrawn no matter how many workers race for it.

### With Default Limits

```python
# Set default limits for APIs that don't provide headers
limiter = RateLimiter(
    default_limits={'requests_per_minute': 60}
)

for user in users:
    response = limiter.request('POST', 'https://api.example.com/notify', json={'user': user})
```

### Wrap Existing Session

```python
import requests
from smartratelimit import RateLimiter

session = requests.Session()
session.headers.update({'Authorization': 'Bearer token'})

limiter = RateLimiter()
limiter.wrap_session(session)

# Now all session requests are rate-limited. The session is still the
# transport, so its headers, cookies, auth, adapters and connection pool
# all continue to apply.
response = session.get('https://api.example.com/data')
```

### Check Rate Limit Status

```python
limiter = RateLimiter()

# Make some requests
limiter.request('GET', 'https://api.github.com/users')

# Check status
status = limiter.get_status('api.github.com')
if status:
    print(f"Remaining: {status.remaining}/{status.limit}")
    print(f"Resets in: {status.reset_in} seconds")
    print(f"Utilization: {status.utilization * 100:.1f}%")
    print(f"Confidence: {status.confidence}")
```

### Know What Was Detected vs. Guessed

Not every API tells you when its window resets. When one reports a limit but no
usable reset header, the window has to be assumed — and a limit of `100` could
mean 100/minute or 100/day. Rather than pass the guess off as a reading,
`status.confidence` says where the number came from:

| Value | Meaning |
| --- | --- |
| `'confirmed'` | The API reported both the limit and its window. |
| `'estimated'` | The API reported a limit but no reset, so the window was assumed (one hour by default). |
| `'configured'` | You set it yourself via `set_limit()` or `default_limits`. |

```python
status = limiter.get_status('api.example.com')
if status.confidence == 'estimated':
    # Replace the guess with the limit from the provider's docs
    limiter.set_limit('api.example.com', limit=100, window='1m')
```

### Manual Rate Limit Configuration

```python
limiter = RateLimiter()

# Manually set rate limits
limiter.set_limit('api.example.com', limit=100, window='1h')
limiter.set_limit('api.another.com', limit=60, window='1m')

# Window formats: '1h', '30m', '60s', '1d'
```

### Per-Endpoint Limits

Most APIs do not have one limit. `GET /search` might allow 10/minute while
`GET /users` allows 100/minute — and a single host-wide bucket forces you to
either throttle everything to the strictest limit or blow straight through the
tight one.

Scope a limit to a path prefix and the narrowest matching rule wins:

```python
limiter = RateLimiter()

limiter.set_limit('api.example.com', limit=100, window='1m')          # host-wide
limiter.set_limit('api.example.com/search', limit=10, window='1m')    # narrower
limiter.set_limit('api.example.com/search/bulk', limit=2, window='1m')  # narrowest

limiter.request('GET', 'https://api.example.com/search/bulk?q=x')  # paced at 2/min
limiter.request('GET', 'https://api.example.com/search?q=x')       # paced at 10/min
limiter.request('GET', 'https://api.example.com/users')            # paced at 100/min
```

Each scope gets its own token bucket, so exhausting `/search` leaves `/users`
untouched. Resolution walks from the full path up to the bare host and uses the
first scope with a stored limit, so a host-wide default and a narrow override
need not know about each other.

Query strings and trailing slashes are ignored when matching — they identify a
request, not a quota. See what is being tracked with:

```python
limiter.list_endpoints()
# ['https://api.example.com/search/bulk',
#  'https://api.example.com/search',
#  'https://api.example.com']
```

A limit you set is marked `confidence="configured"` and **detection will not
overwrite it** — you set it because the headers were absent or wrong, so header
values do not get to overrule you.

### Multi-Dimensional Limits (Tokens, Not Just Requests)

Requests per minute is rarely the constraint that bites on an LLM API. OpenAI
meters **tokens per minute** as well, and a caller comfortably inside its request
budget still gets a 429 once the token budget is spent.

Meter both, and a request has to satisfy both:

```python
limiter = RateLimiter()
limiter.set_limit('api.openai.com', limit=3500, window='1m')                       # RPM
limiter.set_limit('api.openai.com', limit=90000, window='1m', dimension='tokens')  # TPM

response = limiter.request(
    'POST', 'https://api.openai.com/v1/chat/completions',
    json=payload,
    cost={'tokens': estimated_tokens},
)
```

`cost` says what this call spends. Omit it and the call costs one request;
pass a mapping and every dimension named is charged. `requests` defaults to 1
inside a mapping — a 1500-token call is still one request unless you say
otherwise.

**The charge is all-or-nothing.** Both budgets are debited in a single atomic
step, so a request can never spend its request allowance and then be refused for
tokens — a leak that would quietly drain the wrong budget on every rejection.
Refused requests charge nothing at all:

```python
status = limiter.get_status('api.openai.com')
status.dimensions['requests'].remaining   # 3495 after 5 calls
status.dimensions['tokens'].remaining     # 80000
```

A dimension a request doesn't spend never gates it, so `GET /v1/models` with no
`cost` isn't held up by an exhausted token budget.

OpenAI's and Anthropic's token headers are detected automatically, as is the
generic `X-RateLimit-*-Tokens` convention — you only need `set_limit` for APIs
that don't advertise.

### Provider Profiles

Some limits can't be learned from a response in time to be useful. GitHub allows
**60 requests an hour** unauthenticated — low enough that discovering it from the
first response has already cost you a meaningful slice of the budget, and nothing
in a response tells you whether your credentials were accepted before you send
one.

Known hosts are seeded from a built-in profile:

```python
limiter = RateLimiter()                        # seeds GitHub at 60/hour
limiter = RateLimiter(authenticated=True)      # seeds GitHub at 5000/hour
limiter = RateLimiter(use_provider_profiles=False)   # off
```

Seeds are marked `confidence="registry"` and are **replaced the moment the API
reports its own numbers**. They get you a sensible first request, not a permanent
answer.

**The built-in table is deliberately tiny and should stay that way.** Most
providers meter per account tier — OpenAI's RPM depends on which tier you're on,
not on OpenAI — so a baked-in number would be a guess about *your* account, and a
wrong baked-in limit is worse than none. Those providers also send their limits
in headers, which detection already reads. An entry earns its place only when the
number can't be detected in time *and* is a property of the API rather than of
your account.

Your own services are the natural case for adding one:

```python
from smartratelimit import Provider, SeedLimit, register_provider

register_provider('internal.api', Provider(
    name='Internal service',
    reason='Sends no rate-limit headers; limit is in the runbook.',
    scopes={'': [SeedLimit(25, '1m'), SeedLimit(50_000, '1m', dimension='tokens')]},
))
```

### When the Limiter's Storage Goes Down

By default an unreachable Redis fails **open**: the request goes out unpaced and
a warning is logged. That keeps a limiter outage from taking your job down with
it, which is right when the limit is advisory — and wrong when it guards a paid
quota, where sending unpaced traffic is the more expensive failure.

```python
# Raise instead of sending unpaced traffic
limiter = RateLimiter(storage='redis://localhost:6379/0', fail_closed=True)
```

With `fail_closed=True` an unreachable Redis raises `StorageUnavailable` (a
subclass of `RateLimitExceeded`, so existing handlers keep working) at
construction and on every acquire. Either way the failure is logged — it is
never silent.

Note that `redis-py` connects lazily, so the limiter pings Redis at construction
rather than discovering the problem on your first real request.

### Custom Header Mapping

```python
limiter = RateLimiter(
    headers_map={
        'limit': 'X-My-API-Limit',
        'remaining': 'X-My-API-Remaining',
        'reset': 'X-My-API-Reset'
    }
)
```

### Raise Exception Instead of Waiting

```python
limiter = RateLimiter(raise_on_limit=True)

try:
    response = limiter.request('GET', 'https://api.example.com/data')
except RateLimitExceeded as e:
    print(f"Rate limit exceeded: {e}")
```

### Async Support with httpx

```python
import httpx
from smartratelimit import AsyncRateLimiter

async with AsyncRateLimiter() as limiter:
    async with httpx.AsyncClient() as client:
        response = await limiter.arequest_httpx(
            client, 'GET', 'https://api.github.com/users'
        )
        print(response.json())
```

### Async Support with aiohttp

```python
import aiohttp
from smartratelimit import AsyncRateLimiter

async with AsyncRateLimiter() as limiter:
    async with aiohttp.ClientSession() as session:
        response = await limiter.arequest_aiohttp(
            session, 'GET', 'https://api.github.com/users'
        )
        data = await response.json()
        print(data)
```

### Advanced Retry Logic

```python
from smartratelimit import RateLimiter
from smartratelimit.retry import RetryConfig, RetryHandler, RetryStrategy

# Configure retry with exponential backoff
retry_config = RetryConfig(
    max_retries=3,
    strategy=RetryStrategy.EXPONENTIAL,
    base_delay=1.0,
    backoff_factor=2.0,
)

retry_handler = RetryHandler(retry_config)
limiter = RateLimiter()

def make_request():
    return limiter.request('GET', 'https://api.example.com/data')

# Automatically retry on 429, 503, 504
response = retry_handler.retry_sync(make_request)
```

### Metrics Collection

```python
from smartratelimit import RateLimiter
from smartratelimit.metrics import MetricsCollector

limiter = RateLimiter()
metrics = MetricsCollector()

response = limiter.request('GET', 'https://api.github.com/users')
status = limiter.get_status('api.github.com')
metrics.record_request('api.github.com', response.status_code, status)

# Export Prometheus metrics
prometheus_metrics = metrics.export_prometheus()
print(prometheus_metrics)
```

### CLI Tools

```bash
# Probe an endpoint to see what rate limits it advertises
smartratelimit probe https://api.github.com/users/octocat

# Check stored rate limit status (use the same backend your app writes to)
smartratelimit --storage "sqlite:///ratelimit.db" status api.github.com

# Clear stored rate limits for one endpoint
smartratelimit --storage "sqlite:///ratelimit.db" clear api.github.com

# Clear all rate limits
smartratelimit --storage "sqlite:///ratelimit.db" clear
```

## Supported APIs

The library automatically detects rate limits from headers for:

- ✅ GitHub API
- ✅ Stripe API
- ✅ Twitter API
- ✅ OpenAI API
- ✅ Any API using standard `X-RateLimit-*` headers
- ✅ APIs with `Retry-After` headers (429 responses)

## API Reference

### RateLimiter

#### `__init__(storage='memory', default_limits=None, headers_map=None, raise_on_limit=False, retry=None)`

Create a new rate limiter.

**Parameters:**
- `storage` (str): Storage backend. Options:
  - `'memory'` (default): In-memory storage
  - `'sqlite:///path'`: SQLite storage (persistent, single-machine)
  - `'redis://host:port'`: Redis storage (distributed, multi-process)
- `default_limits` (dict): Default limits when headers aren't available. Example: `{'requests_per_minute': 60}`
- `headers_map` (dict): Custom header name mapping
- `raise_on_limit` (bool): If `True`, raise `RateLimitExceeded` instead of waiting
- `retry` (RetryConfig): How to retry a request the server rejects with 429/503/504.
  Defaults to three attempts with jittered exponential backoff.
- `fail_closed` (bool): If `True`, raise `StorageUnavailable` when shared storage
  is unreachable instead of failing open and sending traffic unpaced.
- `use_provider_profiles` (bool): Seed documented limits for known hosts before
  their first response. Default `True`.
- `authenticated` (bool): Whether requests carry credentials. Documented limits
  often differ by orders of magnitude between anonymous and authenticated callers.

`storage` also accepts a ready-made `StorageBackend` instance, for options the
connection string cannot express:

```python
from smartratelimit.storage import RedisStorage

limiter = RateLimiter(
    storage=RedisStorage('redis://localhost:6379/0', key_prefix='myapp:')
)
```

```python
from smartratelimit.retry import RetryConfig, RetryStrategy

limiter = RateLimiter(
    retry=RetryConfig(
        max_retries=5,
        strategy=RetryStrategy.EXPONENTIAL,
        max_delay=30.0,   # also caps how long a Retry-After header can park you
        jitter=0.1,
    )
)
```

A `Retry-After` header on the response wins over the backoff schedule — the
server knows when its window reopens. Both the seconds form and the HTTP-date
form are honoured, capped at `max_delay`. `RetryStrategy.NONE` means one
attempt, no retries.

#### `request(method, url, cost=None, **kwargs) -> requests.Response`

Make a rate-limited HTTP request.

**Parameters:**
- `method` (str): HTTP method (GET, POST, PUT, DELETE, PATCH)
- `url` (str): Request URL
- `cost` (dict | number | None): What this request spends. `None` is one request;
  a number is that many requests; a mapping like `{'tokens': 1500}` charges other
  metered dimensions too, with `requests` defaulting to 1. Every dimension named
  is charged atomically together.
- `**kwargs`: Additional arguments passed to `requests.request()`

**Returns:** `requests.Response` object

#### `wrap_session(session: requests.Session) -> None`

Wrap an existing `requests.Session` with rate limiting, in place. The session
remains the transport: its headers, cookies, auth, adapters, proxies and
connection pool are all still used — only the scheduling of the call is taken
over. Wrapping the same session twice is a no-op.

#### `get_status(endpoint: str) -> RateLimitStatus | None`

Get current rate limit status for an endpoint. Accepts a bare domain, a full
URL, or a domain plus path prefix; a bare domain matches whichever scheme was
actually stored, so an http-only API is not missed.

**Returns:** `RateLimitStatus` object or `None` if no info available

#### `list_endpoints() -> list[str]`

Every endpoint scope with a stored rate limit, most specific first.

#### `set_limit(endpoint: str, limit: int, window: str = '1h') -> None`

Manually set rate limit for an endpoint.

**Parameters:**
- `endpoint`: Endpoint URL or domain, optionally narrowed by a path prefix
  (`'api.example.com/search'`). The narrowest matching scope wins.
- `limit`: Maximum units of `dimension` per window
- `dimension`: What is being metered, default `'requests'`. Call again with
  another name (`'tokens'`) to add a second budget to the same scope.
- `window`: Time window ('1h', '1m', '30s', '1d'). **Raises `ValueError`** if it
  is not a positive whole number plus `d`/`h`/`m`/`s` — a mistyped `'1.5h'`
  silently becoming one hour is worse than a loud error at startup.

#### `clear(endpoint: str | None = None) -> None`

Clear stored rate limit data.

**Parameters:**
- `endpoint`: Specific endpoint to clear, or `None` to clear all

### RateLimitStatus

Status information about current rate limits.

**Properties:**
- `endpoint` (str): Endpoint URL
- `limit` (int): Total rate limit
- `remaining` (int): Remaining requests
- `reset_time` (datetime): When the limit resets
- `window` (timedelta): Time window for the limit
- `confidence` (str): `'confirmed'`, `'estimated'`, `'configured'` or `'registry'`
- `dimensions` (dict[str, LimitDimension]): Every metered dimension, keyed by
  name, including `requests`
- `reset_in` (float): Seconds until reset (property)
- `is_exceeded` (bool): Whether limit is exceeded (property)
- `utilization` (float): Utilization percentage 0.0-1.0 (property)

## Examples

### Web Scraper

```python
from smartratelimit import RateLimiter

limiter = RateLimiter()

for url in urls:
    response = limiter.request('GET', url)
    html = response.text
    # Process HTML...
```

### API Integration in FastAPI

```python
from fastapi import FastAPI
from smartratelimit import RateLimiter

app = FastAPI()
limiter = RateLimiter()

@app.get("/notify")
def notify_user(user_id: str):
    response = limiter.request(
        'POST',
        'https://api.sendgrid.com/v3/mail/send',
        json={'to': user_id, 'message': 'Hello!'}
    )
    return {"status": "sent"}
```

### Batch Processing

```python
from smartratelimit import RateLimiter

limiter = RateLimiter(default_limits={'requests_per_minute': 60})

results = []
for item in items:
    response = limiter.request('POST', 'https://api.example.com/process', json=item)
    results.append(response.json())
```

## Roadmap

### v0.1.0 - MVP
- ✅ Basic rate limiting with token bucket algorithm
- ✅ Automatic header detection
- ✅ In-memory storage
- ✅ `requests` library integration
- ✅ Status monitoring

### v0.2.0 - Production Ready
- ✅ SQLite persistence
- ✅ Redis backend for distributed applications
- ✅ Multi-process support
- ✅ Performance benchmarks
- ✅ Comprehensive test coverage

### v0.3.0 (Current) - Advanced Features
- ✅ `httpx` and `aiohttp` async support
- ✅ Advanced retry logic with configurable strategies
- ✅ CLI tools (status, clear, probe commands)
- ✅ Monitoring/metrics export (Prometheus format)

## Contributing

Contributions are welcome! Please read [CONTRIBUTING.md](https://github.com/Olaverse-Labs/smartratelimit/blob/main/CONTRIBUTING.md) for details on our code of conduct and the process for submitting pull requests.

## License

This project is licensed under the Apache License 2.0.

See the [LICENSE](https://github.com/Olaverse-Labs/smartratelimit/blob/main/LICENSE) file for the full license text.

## Documentation

Full documentation lives at **[olaverse-labs.github.io/smartratelimit](https://olaverse-labs.github.io/smartratelimit/)**:

- 📖 [Quick Start](https://olaverse-labs.github.io/smartratelimit/quickstart/) - Get started in 5 minutes
- 🧠 [How it works](https://olaverse-labs.github.io/smartratelimit/concepts/) - Detection, token bucket, storage
- 💾 [Which storage backend?](https://olaverse-labs.github.io/smartratelimit/choosing/) - Memory vs SQLite vs Redis
- 📡 [Detection & Headers](https://olaverse-labs.github.io/smartratelimit/detection/) - Supported headers and custom mapping
- ⚡ [Async Guide](https://olaverse-labs.github.io/smartratelimit/async/) - httpx and aiohttp
- 🔄 [Retry Strategies](https://olaverse-labs.github.io/smartratelimit/retry/) - Backoff configuration
- 📊 [Metrics](https://olaverse-labs.github.io/smartratelimit/metrics/) - Prometheus and JSON export
- 🛠️ [CLI](https://olaverse-labs.github.io/smartratelimit/cli/) - `status`, `probe`, `clear`
- 💻 [Recipes](https://olaverse-labs.github.io/smartratelimit/examples/) - Batch jobs, scrapers, Celery, FastAPI
- 📋 [API Reference](https://olaverse-labs.github.io/smartratelimit/api/) - Every class and method

The site is built with MkDocs from the [`docs/`](docs/) directory and deploys on push to `main`.

## Support

- 📖 [Documentation](https://olaverse-labs.github.io/smartratelimit/)
- 🐛 [Issue Tracker](https://github.com/Olaverse-Labs/smartratelimit/issues)
- 💬 [Discussions](https://github.com/Olaverse-Labs/smartratelimit/discussions)

## Acknowledgments

Inspired by the need for a simple, automatic rate limiting solution that works with any API without configuration.

