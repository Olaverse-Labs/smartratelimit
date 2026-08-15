# Quick Start

Everything on this page runs against public APIs that need no key.

## Install

```bash
pip install smartratelimit
```

## 1. Replace your request call

```python
from smartratelimit import RateLimiter

limiter = RateLimiter()

response = limiter.request("GET", "https://api.github.com/users/octocat")
print(response.json()["name"])
```

`limiter.request()` takes the same arguments as `requests.request()` — `params`, `json`, `headers`, `timeout`, everything — and returns a plain `requests.Response`. The difference is what happens *before* the call: if the endpoint's remaining quota has run out, the limiter sleeps until a token is available instead of sending a request that would come back 429.

## 2. Loop without thinking about it

```python
from smartratelimit import RateLimiter

limiter = RateLimiter()

names = ["Michael", "Sarah", "Alex", "Jordan", "Casey"]
for name in names:
    response = limiter.request("GET", "https://api.agify.io", params={"name": name})
    data = response.json()
    print(f"{name}: predicted age {data['age']}")
```

The first response carries `X-RateLimit-Limit` / `X-RateLimit-Remaining` / `X-RateLimit-Reset`. From that point the limiter knows the quota and paces the rest of the loop to fit inside it.

## 3. Check where you stand

```python
status = limiter.get_status("api.agify.io")

if status:
    print(f"Limit:       {status.limit}")
    print(f"Remaining:   {status.remaining}")
    print(f"Utilization: {status.utilization * 100:.1f}%")
    print(f"Resets in:   {status.reset_in:.0f}s")
    print(f"Exceeded:    {status.is_exceeded}")
```

`get_status()` returns `None` when nothing has been detected or set for that endpoint yet — a bare domain like `api.agify.io` works, and so does a full URL. See [`RateLimitStatus`](api.md#ratelimitstatus).

## 4. Survive a restart

In-memory state dies with the process. Point at a SQLite file and it doesn't:

```python
limiter = RateLimiter(storage="sqlite:///ratelimit.db")

response = limiter.request("GET", "https://api.github.com/users/octocat")
# Quota, reset time and bucket level are written to ratelimit.db.
# Start the script again in a minute and it picks up where it left off.
```

For workers that must share one quota across processes or machines, use Redis:

```python
limiter = RateLimiter(storage="redis://localhost:6379/0")
```

Full comparison: [Which storage backend?](choosing.md)

## 5. APIs that send no headers

Plenty of APIs document a limit but never advertise it in a response header. Tell the limiter what it is:

```python
# A fallback, used only when nothing is detected from headers
limiter = RateLimiter(default_limits={"requests_per_minute": 60})

# Or pin a specific endpoint explicitly
limiter.set_limit("api.example.com", limit=1000, window="1h")
```

`default_limits` accepts **one** of `requests_per_second`, `requests_per_minute`, or `requests_per_hour` — if several are present, the shortest window wins and the others are ignored. `window` strings are a whole number plus a unit: `30s`, `15m`, `1h`, `1d`.

## 6. Rate-limit an existing session

Already built a `requests.Session` with your auth headers? Wrap it in place:

```python
import requests
from smartratelimit import RateLimiter

session = requests.Session()
session.headers.update({"Authorization": "Bearer <token>"})

limiter = RateLimiter()
limiter.wrap_session(session)

# Now paced by the limiter
response = session.request("GET", "https://api.example.com/data")
```

!!! warning "Wrapping replaces `session.request` only"
    `wrap_session()` patches the session's `request` method in place and returns
    `None` — keep using the same `session` object, not a return value. The
    limiter issues the wrapped calls through **its own** internal session, so
    headers, auth and cookies you set on yours are not carried over. If your
    session is configured, pass those settings per call instead:

    ```python
    limiter.request("GET", url, headers={"Authorization": "Bearer <token>"})
    ```

## Where next

- [How it works](concepts.md) — the detection → bucket → storage path, in one page
- [Detection & Headers](detection.md) — which headers are recognised, and mapping your own
- [Async](async.md) — the same behaviour under `httpx` and `aiohttp`
- [Recipes](examples.md) — batch jobs, scrapers, FastAPI, Celery
