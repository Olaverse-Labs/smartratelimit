# smartratelimit

[![Python Version](https://img.shields.io/badge/python-3.8+-blue.svg)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](https://github.com/olastephen/smartratelimit/blob/main/LICENSE)
[![PyPI version](https://badge.fury.io/py/smartratelimit.svg)](https://badge.fury.io/py/smartratelimit)
[![PyPI downloads](https://img.shields.io/pypi/dm/smartratelimit.svg)](https://pypi.org/project/smartratelimit/)

A Python library that automatically manages API rate limits, preventing 429 errors and optimizing API usage without requiring developers to manually track or implement rate limiting logic.

## Features

- 🚀 **Automatic Detection**: Automatically detects rate limits from HTTP response headers
- 🔄 **Zero Configuration**: Works out of the box with most APIs
- 💾 **Persistent State**: Supports in-memory, SQLite, and Redis storage
- 🔀 **Multi-Process Safe**: Share rate limits across multiple processes with Redis
- 🎯 **Smart Waiting**: Automatically waits when limits are reached
- 📊 **Status Monitoring**: Check current rate limit status anytime
- 🔌 **Easy Integration**: Works with `requests`, `httpx`, and `aiohttp`
- 🔄 **Advanced Retry**: Configurable retry strategies with exponential backoff
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

# Now all session requests are rate-limited
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
```

### Manual Rate Limit Configuration

```python
limiter = RateLimiter()

# Manually set rate limits
limiter.set_limit('api.example.com', limit=100, window='1h')
limiter.set_limit('api.another.com', limit=60, window='1m')

# Window formats: '1h', '30m', '60s', '1d'
```

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
# Check rate limit status
smartratelimit status --endpoint api.github.com

# Probe endpoint for rate limits
smartratelimit probe https://api.github.com/users

# Clear stored rate limits
smartratelimit clear --endpoint api.github.com

# Clear all rate limits
smartratelimit clear
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

#### `__init__(storage='memory', default_limits=None, headers_map=None, raise_on_limit=False)`

Create a new rate limiter.

**Parameters:**
- `storage` (str): Storage backend. Options:
  - `'memory'` (default): In-memory storage
  - `'sqlite:///path'`: SQLite storage (persistent, single-machine)
  - `'redis://host:port'`: Redis storage (distributed, multi-process)
- `default_limits` (dict): Default limits when headers aren't available. Example: `{'requests_per_minute': 60}`
- `headers_map` (dict): Custom header name mapping
- `raise_on_limit` (bool): If `True`, raise `RateLimitExceeded` instead of waiting

#### `request(method, url, **kwargs) -> requests.Response`

Make a rate-limited HTTP request.

**Parameters:**
- `method` (str): HTTP method (GET, POST, PUT, DELETE, PATCH)
- `url` (str): Request URL
- `**kwargs`: Additional arguments passed to `requests.request()`

**Returns:** `requests.Response` object

#### `wrap_session(session: requests.Session) -> None`

Wrap an existing `requests.Session` with rate limiting.

#### `get_status(endpoint: str) -> RateLimitStatus | None`

Get current rate limit status for an endpoint.

**Returns:** `RateLimitStatus` object or `None` if no info available

#### `set_limit(endpoint: str, limit: int, window: str = '1h') -> None`

Manually set rate limit for an endpoint.

**Parameters:**
- `endpoint`: Endpoint URL or domain
- `limit`: Maximum number of requests
- `window`: Time window ('1h', '1m', '30s', '1d')

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

Contributions are welcome! Please read [CONTRIBUTING.md](https://github.com/olastephen/smartratelimit/blob/main/CONTRIBUTING.md) for details on our code of conduct and the process for submitting pull requests.

## License

This project is licensed under the Apache License 2.0.

See the [LICENSE](https://github.com/olastephen/smartratelimit/blob/main/LICENSE) file for the full license text.

## Documentation

Comprehensive documentation is available in the `docs/` directory:

- 📖 [Quick Start Guide](https://github.com/olastephen/smartratelimit/blob/main/docs/QUICK_START.md) - Get started in 5 minutes
- 📚 [Complete Tutorial](https://github.com/olastephen/smartratelimit/blob/main/docs/TUTORIAL.md) - Step-by-step guide
- 📋 [API Reference](https://github.com/olastephen/smartratelimit/blob/main/docs/API_REFERENCE.md) - Complete API documentation
- 💻 [Examples](https://github.com/olastephen/smartratelimit/blob/main/docs/EXAMPLES.md) - Real-world examples with free APIs
- 💾 [Storage Backends](https://github.com/olastephen/smartratelimit/blob/main/docs/STORAGE_BACKENDS.md) - SQLite and Redis guide
- ⚡ [Async Guide](https://github.com/olastephen/smartratelimit/blob/main/docs/ASYNC_GUIDE.md) - Async/await usage
- 🔄 [Retry Strategies](https://github.com/olastephen/smartratelimit/blob/main/docs/RETRY_STRATEGIES.md) - Advanced retry logic
- 📊 [Metrics Guide](https://github.com/olastephen/smartratelimit/blob/main/docs/METRICS_GUIDE.md) - Collecting and exporting metrics
- 🛠️ [CLI Guide](https://github.com/olastephen/smartratelimit/blob/main/docs/CLI_GUIDE.md) - Command-line tools
- 🎯 [Advanced Features](https://github.com/olastephen/smartratelimit/blob/main/docs/ADVANCED_FEATURES.md) - Advanced patterns

## Support

- 📖 [Documentation](https://github.com/olastephen/smartratelimit)
- 🐛 [Issue Tracker](https://github.com/olastephen/smartratelimit/issues)
- 💬 [Discussions](https://github.com/olastephen/smartratelimit/discussions)

## Acknowledgments

Inspired by the need for a simple, automatic rate limiting solution that works with any API without configuration.

