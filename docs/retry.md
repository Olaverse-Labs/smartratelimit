# Retry Strategies

Pacing prevents most 429s. Retrying handles the rest — and the 503s and 504s that have nothing to do with rate limits.

`RetryHandler` wraps **any callable**, not just this library's requests. It inspects the returned object for a status code and decides whether to go again.

```python
from smartratelimit import RateLimiter
from smartratelimit.retry import RetryConfig, RetryHandler, RetryStrategy

limiter = RateLimiter()
handler = RetryHandler(RetryConfig(max_retries=3, strategy=RetryStrategy.EXPONENTIAL))

response = handler.retry_sync(
    lambda: limiter.request("GET", "https://api.example.com/data")
)
```

## Configuration

```python
RetryConfig(
    max_retries=3,                        # attempts after the first call
    strategy=RetryStrategy.EXPONENTIAL,
    base_delay=1.0,                       # seconds
    max_delay=60.0,                       # ceiling on any single delay
    backoff_factor=2.0,                   # exponential only
    retry_on_status=[429, 503, 504],      # default
)
```

`RetryHandler()` with no config uses exactly these defaults.

## The four strategies

| Strategy | Delay before attempt *n* | With defaults |
|---|---|---|
| `EXPONENTIAL` | `base_delay × factor^(n-1)` | 1s, 2s, 4s, 8s… |
| `LINEAR` | `base_delay × n` | 1s, 2s, 3s, 4s… |
| `FIXED` | `base_delay` | 1s, 1s, 1s… |
| `NONE` | `0` | immediate retries |

Every delay is capped at `max_delay`.

```python
from smartratelimit.retry import RetryConfig, RetryStrategy

# Patient: a rate limit that resets on a long window
RetryConfig(strategy=RetryStrategy.EXPONENTIAL, base_delay=2.0, max_delay=120.0)

# Predictable: a flaky upstream where jitter isn't the issue
RetryConfig(strategy=RetryStrategy.FIXED, base_delay=5.0, max_retries=5)

# Impatient: retry only transient server errors, never rate limits
RetryConfig(strategy=RetryStrategy.LINEAR, retry_on_status=[502, 503, 504])
```

!!! note "Jitter is opt-in"
    Delays are deterministic unless you ask for jitter, so a configured schedule
    stays exactly predictable. If many workers hit the same limit at the same
    moment they would otherwise retry in lockstep and collide again — pass
    `jitter=0.1` to spread them by up to 10% either way. `RateLimiter` and
    `AsyncRateLimiter` default their own retries to `jitter=0.1`.

## Which statuses retry

`retry_on_status` decides. The default `[429, 503, 504]` covers "you're going too fast" and "the upstream is briefly unwell". Anything else — a 400, a 404, a 500 — is returned immediately, because retrying it will not help.

`RateLimiter.request()` already retries these statuses on its own, using the same `RetryConfig` type — pass one as `RateLimiter(retry=...)` to configure it. Reach for a standalone `RetryHandler` when you need to retry something that is not a limiter request, or want a different schedule around one particular call.

`RetryStrategy.NONE` means a single attempt: retrying with a zero delay would just hammer an endpoint that already said no.

## Exceptions are retried too

If the callable raises — a connection reset, a read timeout — the handler waits and tries again on the same schedule. After the last attempt, the final exception is re-raised unchanged:

```python
import requests

handler = RetryHandler(RetryConfig(max_retries=3, base_delay=1.0))

try:
    response = handler.retry_sync(
        lambda: limiter.request("GET", url, timeout=5)
    )
except requests.Timeout:
    # all four attempts timed out
    ...
```

When retries are exhausted on a *status code* (rather than an exception), the last response is returned rather than raised — check `response.status_code` afterwards.

```python
response = handler.retry_sync(lambda: limiter.request("GET", url))

if response.status_code == 429:
    # still rate limited after every retry
    ...
```

## Async

`retry_async` is the same logic with `await asyncio.sleep()` between attempts. It reads `.status_code` or `.status`, so both httpx and aiohttp responses work:

```python
import httpx
from smartratelimit import AsyncRateLimiter
from smartratelimit.retry import RetryConfig, RetryHandler, RetryStrategy

handler = RetryHandler(RetryConfig(max_retries=3, strategy=RetryStrategy.EXPONENTIAL))

async with AsyncRateLimiter() as limiter:
    async with httpx.AsyncClient() as client:

        async def call():
            return await limiter.arequest_httpx(client, "GET", url)

        response = await handler.retry_async(call)
```

Pass a coroutine **function**, not an already-awaited coroutine — the handler calls it once per attempt.

## Arguments

Both methods forward extra arguments to the callable, which is often tidier than a closure:

```python
def fetch(user, timeout=10):
    return limiter.request("GET", f"https://api.github.com/users/{user}", timeout=timeout)

response = handler.retry_sync(fetch, "octocat", timeout=5)
```

## Picking numbers

- **`max_retries`** — 3 is a reasonable default. Past 5, you are usually queuing work that should be re-run later, not held in memory.
- **`base_delay`** — start near the API's typical reset granularity. For a per-second limit, 1s; for a per-hour quota, retries won't save you and you want [pacing](concepts.md) plus a job queue instead.
- **`max_delay`** — cap it below your caller's own timeout. An exponential schedule with a 60s ceiling and 5 retries can wait over two minutes in total.

## Total wait, at a glance

Worst case with the defaults (`EXPONENTIAL`, `base_delay=1`, `factor=2`, `max_retries=3`):

```
attempt 1  →  fail  →  wait 1s
attempt 2  →  fail  →  wait 2s
attempt 3  →  fail  →  wait 4s
attempt 4  →  result returned (or exception raised)
```

Seven seconds of waiting, four calls.
