# Async

`AsyncRateLimiter` is the same detection, token bucket and storage as [`RateLimiter`](api.md#ratelimiter) — with `await asyncio.sleep()` in place of `time.sleep()`, so waiting on one endpoint doesn't block the event loop.

```bash
pip install smartratelimit[httpx]     # or [aiohttp], or [all]
```

There is one method per client library. You bring the client; the limiter paces the call.

## httpx

```python
import asyncio
import httpx
from smartratelimit import AsyncRateLimiter


async def main():
    async with AsyncRateLimiter() as limiter:
        async with httpx.AsyncClient() as client:
            response = await limiter.arequest_httpx(
                client, "GET", "https://api.github.com/users/octocat"
            )
            print(response.json()["name"])


asyncio.run(main())
```

`arequest_httpx(client, method, url, **kwargs)` forwards everything after the URL to `client.request()` and returns the real `httpx.Response`.

## aiohttp

```python
import asyncio
import aiohttp
from smartratelimit import AsyncRateLimiter


async def main():
    async with AsyncRateLimiter() as limiter:
        async with aiohttp.ClientSession() as session:
            response = await limiter.arequest_aiohttp(
                session, "GET", "https://api.github.com/users/octocat"
            )
            data = await response.json()
            print(data["name"])


asyncio.run(main())
```

!!! note "The aiohttp return value is a wrapper, not a `ClientResponse`"
    aiohttp responses are only valid inside their `async with` block, which the
    limiter has already exited by the time you get the value back. So the body
    is read first and handed back in a small wrapper exposing `await .json()`,
    `await .text()`, `await .read()`, plus `.status`, `.status_code`,
    `.headers` and `.url`. Streaming and `.content` are not available — use
    httpx if you need to stream.

## Running requests concurrently

The limiter's whole job is to keep concurrency from becoming a 429:

```python
import asyncio
import httpx
from smartratelimit import AsyncRateLimiter

USERS = ["octocat", "torvalds", "gvanrossum", "kennethreitz", "mitsuhiko"]


async def fetch(limiter, client, user):
    response = await limiter.arequest_httpx(
        client, "GET", f"https://api.github.com/users/{user}"
    )
    return user, response.status_code


async def main():
    async with AsyncRateLimiter(storage="sqlite:///ratelimit.db") as limiter:
        async with httpx.AsyncClient() as client:
            results = await asyncio.gather(
                *(fetch(limiter, client, u) for u in USERS)
            )
    for user, status in results:
        print(user, status)


asyncio.run(main())
```

Tasks that need to wait do so with `asyncio.sleep`, so the others keep running.

!!! warning "Pacing is approximate under heavy concurrency"
    The bucket check and the token consumption are not one atomic step, so a
    large `gather()` can let several tasks past the check at once and overshoot
    slightly. It is a soft pacer, not a semaphore. If you need a hard ceiling on
    in-flight requests, pair it with one:

    ```python
    sem = asyncio.Semaphore(10)

    async def fetch(limiter, client, user):
        async with sem:
            return await limiter.arequest_httpx(client, "GET", url)
    ```

## Storage, status, and limits

The constructor takes the same four arguments as the sync limiter, and `get_status()`, `set_limit()` and `clear()` are identical — **synchronous** methods, called without `await`:

```python
async with AsyncRateLimiter(
    storage="redis://localhost:6379/0",
    default_limits={"requests_per_minute": 120},
    raise_on_limit=False,
) as limiter:
    ...
    status = limiter.get_status("api.github.com")   # no await
    print(status.remaining, status.limit)
```

!!! note "Backend I/O is blocking"
    SQLite and Redis are accessed with their synchronous drivers, so each
    request does a small blocking read/write on the event loop thread. For
    ordinary paced workloads this is negligible; for very high request rates
    prefer `memory` in-process, or move the work to a thread pool.

## Sharing state with sync code

An async worker and a sync management command can point at the same backend and see the same quota:

```python
# async worker
async with AsyncRateLimiter(storage="redis://localhost:6379/0") as limiter:
    ...

# sync CLI or admin script, same Redis
from smartratelimit import RateLimiter
RateLimiter(storage="redis://localhost:6379/0").get_status("api.github.com")
```

## Retries

`RetryHandler` has an async counterpart that awaits between attempts:

```python
from smartratelimit.retry import RetryConfig, RetryHandler, RetryStrategy

handler = RetryHandler(RetryConfig(max_retries=3, strategy=RetryStrategy.EXPONENTIAL))

async def call():
    return await limiter.arequest_httpx(client, "GET", url)

response = await handler.retry_async(call)
```

See [Retry Strategies](retry.md).

## `raise_on_limit`

Same semantics as the sync limiter: instead of awaiting, it raises `RateLimitExceeded` (imported from `smartratelimit`) as soon as the bucket is empty.

```python
from smartratelimit import AsyncRateLimiter, RateLimitExceeded

async with AsyncRateLimiter(raise_on_limit=True) as limiter:
    try:
        response = await limiter.arequest_httpx(client, "GET", url)
    except RateLimitExceeded:
        return fallback_response()
```
