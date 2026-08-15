# Recipes

Complete, runnable patterns. Public APIs where possible; where a key is needed, it's marked.

## Batch enrichment

The archetypal use: a list of things, one API call each, a quota that won't cover a sprint.

```python
from smartratelimit import RateLimiter

limiter = RateLimiter(storage="sqlite:///ratelimit.db")

names = ["Michael", "Sarah", "Alex", "Jordan", "Casey", "Riley", "Sam"]
results = {}

for name in names:
    response = limiter.request("GET", "https://api.agify.io", params={"name": name})
    results[name] = response.json().get("age")

    status = limiter.get_status("api.agify.io")
    if status:
        print(f"{name:10} age={results[name]}  ({status.remaining}/{status.limit} left)")

print(results)
```

SQLite storage is what makes this restartable: kill it halfway, run it again, and the second run starts with the quota it actually has left rather than assuming a full window.

## Resumable batch

Pair the limiter with a record of what's already done, and a long job survives interruption:

```python
import json
from pathlib import Path

from smartratelimit import RateLimiter

DONE = Path("done.json")
limiter = RateLimiter(storage="sqlite:///ratelimit.db")

done = json.loads(DONE.read_text()) if DONE.exists() else {}
todo = [u for u in ALL_USERS if u not in done]

try:
    for user in todo:
        response = limiter.request("GET", f"https://api.github.com/users/{user}")
        if response.status_code == 200:
            done[user] = response.json()["public_repos"]
finally:
    DONE.write_text(json.dumps(done))

print(f"{len(done)}/{len(ALL_USERS)} complete")
```

## Several APIs, one limiter

Quotas are tracked per host, so a single limiter handles a fan-out across services without them interfering:

```python
from smartratelimit import RateLimiter

limiter = RateLimiter(storage="sqlite:///ratelimit.db")

sources = [
    ("https://api.github.com/users/octocat", {}),
    ("https://api.agify.io", {"name": "Michael"}),
    ("https://dog.ceo/api/breeds/image/random", {}),
]

for url, params in sources:
    response = limiter.request("GET", url, params=params)
    print(url, response.status_code)

for host in ["api.github.com", "api.agify.io", "dog.ceo"]:
    status = limiter.get_status(host)
    print(host, "→", f"{status.remaining}/{status.limit}" if status else "no limit detected")
```

## An API with a key and no headers

Many commercial APIs document a quota and advertise nothing. State it once:

```python
import os

from smartratelimit import RateLimiter

API_KEY = os.environ["OPENWEATHER_API_KEY"]

limiter = RateLimiter(storage="sqlite:///ratelimit.db")
limiter.set_limit("api.openweathermap.org", limit=60, window="1m")   # free tier

cities = ["London", "Lagos", "Tokyo", "Lima", "Oslo"]

for city in cities:
    response = limiter.request(
        "GET",
        "https://api.openweathermap.org/data/2.5/weather",
        params={"q": city, "appid": API_KEY, "units": "metric"},
    )
    if response.ok:
        data = response.json()
        print(f"{city:6} {data['main']['temp']:5.1f}°C  {data['weather'][0]['description']}")
```

`set_limit` is per endpoint and explicit. `default_limits={"requests_per_minute": 60}` does the same job as a blanket fallback for every endpoint with nothing detected.

## RapidAPI

RapidAPI sends `X-RateLimit-Requests-*` headers, which the detector already understands — so the plan limit is picked up automatically:

```python
import os

from smartratelimit import RateLimiter

limiter = RateLimiter(storage="sqlite:///ratelimit.db")

headers = {
    "X-RapidAPI-Key": os.environ["RAPIDAPI_KEY"],
    "X-RapidAPI-Host": "example-api.p.rapidapi.com",
}

for email in emails:
    response = limiter.request(
        "GET",
        "https://example-api.p.rapidapi.com/validate",
        headers=headers,
        params={"email": email},
    )
    print(email, response.json())

status = limiter.get_status("example-api.p.rapidapi.com")
print(f"Plan quota: {status.remaining}/{status.limit} remaining")
```

Pass auth headers per call — a limiter's internal session is not your session. See the note in the [quickstart](quickstart.md#6-rate-limit-an-existing-session).

## Scraper with retries

Pacing for the quota, retries for the flakiness:

```python
from smartratelimit import RateLimiter
from smartratelimit.retry import RetryConfig, RetryHandler, RetryStrategy

limiter = RateLimiter(
    storage="sqlite:///scraper.db",
    default_limits={"requests_per_second": 2},      # be polite to unmarked sites
)
handler = RetryHandler(
    RetryConfig(max_retries=3, strategy=RetryStrategy.EXPONENTIAL, max_delay=30.0)
)

for url in urls:
    response = handler.retry_sync(
        lambda u=url: limiter.request("GET", u, timeout=10)
    )
    if response.ok:
        save(url, response.text)
    else:
        print(f"gave up on {url}: {response.status_code}")
```

## Celery workers sharing one quota

This is the case memory storage silently gets wrong: ten workers, ten private buckets, ten times the intended request rate.

```python
# tasks.py
from celery import Celery
from smartratelimit import RateLimiter

app = Celery("tasks", broker="redis://localhost:6379/1")

# One shared bucket across every worker on every machine
limiter = RateLimiter(storage="redis://localhost:6379/0")


@app.task(bind=True, max_retries=3)
def enrich(self, user_id):
    response = limiter.request("GET", f"https://api.example.com/users/{user_id}")
    if response.status_code == 429:
        raise self.retry(countdown=60)
    return response.json()
```

The same shape works under Gunicorn: build the limiter at module scope with a Redis URL and every worker process draws from one quota.

## FastAPI: fail fast instead of waiting

Inside a request handler, blocking for 30 seconds is worse than returning an error. Use `raise_on_limit=True`:

```python
from fastapi import FastAPI, HTTPException
from smartratelimit import RateLimiter, RateLimitExceeded

app = FastAPI()
limiter = RateLimiter(storage="redis://localhost:6379/0", raise_on_limit=True)


@app.get("/lookup/{user}")
def lookup(user: str):
    try:
        response = limiter.request("GET", f"https://api.github.com/users/{user}")
    except RateLimitExceeded:
        raise HTTPException(
            status_code=503,
            detail="Upstream quota exhausted, try again shortly",
            headers={"Retry-After": "60"},
        )
    return response.json()
```

In an `async def` handler, use [`AsyncRateLimiter`](async.md) — the sync limiter's wait blocks the event loop.

## Async fan-out

```python
import asyncio

import httpx
from smartratelimit import AsyncRateLimiter

USERS = ["octocat", "torvalds", "gvanrossum", "kennethreitz", "mitsuhiko"]


async def main():
    async with AsyncRateLimiter(storage="sqlite:///ratelimit.db") as limiter:
        async with httpx.AsyncClient(timeout=10) as client:

            async def fetch(user):
                r = await limiter.arequest_httpx(
                    client, "GET", f"https://api.github.com/users/{user}"
                )
                return user, r.json().get("public_repos")

            for user, repos in await asyncio.gather(*map(fetch, USERS)):
                print(f"{user:15} {repos} repos")


asyncio.run(main())
```

## Quota-aware scheduling

Rather than letting the limiter sleep, ask how much room you have and shape the work:

```python
from smartratelimit import RateLimiter

limiter = RateLimiter(storage="sqlite:///ratelimit.db")
limiter.request("GET", "https://api.github.com/rate_limit")   # cheap, sets the quota

status = limiter.get_status("api.github.com")

if status and status.remaining < len(work):
    print(f"Only {status.remaining} calls left, resetting in {status.reset_in / 60:.0f} min")
    work = work[: status.remaining]      # do what fits, queue the rest
```

## Metrics alongside the work

```python
from smartratelimit import RateLimiter
from smartratelimit.metrics import MetricsCollector

limiter = RateLimiter(storage="redis://localhost:6379/0")
metrics = MetricsCollector()


def call(url, **kwargs):
    host = url.split("/")[2]
    response = limiter.request("GET", url, **kwargs)
    metrics.record_request(host, response.status_code, limiter.get_status(host))
    return response


for user in users:
    call(f"https://api.github.com/users/{user}")

open("/var/lib/node_exporter/ratelimit.prom", "w").write(metrics.export_prometheus())
```

Full details in [Metrics & Monitoring](metrics.md).

## Testing code that uses a limiter

Use an in-process SQLite database so each test starts clean and nothing hits the filesystem:

```python
import pytest
from smartratelimit import RateLimiter


@pytest.fixture
def limiter():
    return RateLimiter(storage="sqlite:///:memory:")


def test_respects_manual_limit(limiter, requests_mock):
    requests_mock.get("https://api.example.com/data", json={"ok": True})
    limiter.set_limit("api.example.com", limit=10, window="1m")

    limiter.request("GET", "https://api.example.com/data")

    status = limiter.get_status("api.example.com")
    assert status.limit == 10
```

Set generous limits in tests — a real limit with a short window will make the suite genuinely sleep.
