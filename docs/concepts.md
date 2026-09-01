# How it works

One `limiter.request()` call does four things. Understanding them explains every behaviour on the rest of the site.

```
                 ┌──────────────────────────────────────────┐
  request(...)   │ 1. look up stored quota for this endpoint │
       │         └──────────────────────────────────────────┘
       ▼                          │
┌──────────────┐                  ▼
│ 2. token     │◄──── refill at limit/window tokens per second
│    bucket    │      empty?  →  sleep until one is available
└──────────────┘
       │
       ▼
┌──────────────────┐     ┌─────────────────────────────────────┐
│ 3. send request  │────►│ 4. read rate-limit headers from the │
└──────────────────┘     │    response, update stored quota    │
                         └─────────────────────────────────────┘
```

## 1. The endpoint key

State is tracked per **scheme + host**, not per path:

```python
"https://api.github.com/users/octocat"  ->  "https://api.github.com"
"https://api.github.com/repos/a/b"      ->  "https://api.github.com"
```

Both URLs above share one quota, which matches how most APIs actually count. It also means two APIs on the same host share a bucket, and an API with per-route limits is tracked at its tightest observed value rather than per route.

Bare domains are accepted anywhere an endpoint is expected (`get_status`, `set_limit`, `clear`) and are normalised to `https://`.

## 2. The token bucket

A [token bucket](https://en.wikipedia.org/wiki/Token_bucket) holds up to `limit` tokens and refills continuously at:

```
refill_rate = limit / window_in_seconds   # tokens per second
```

Each request consumes one token. If the bucket is empty, `wait_time()` says how long until the next token lands, and the limiter sleeps exactly that long.

The practical effect is **pacing, not stalling**. Given "5000 requests per hour", a naive client fires 5000 requests in three minutes and then sits dead for 57. The bucket refills at 1.39 tokens/second, so requests are spread across the hour and the quota is never actually exhausted.

Two levers change this:

- Every response with rate-limit headers **resets the bucket level** to the server's `remaining` value. The server is the authority; local accounting is only a prediction between responses.
- The bucket's `capacity` and `refill_rate` are recomputed whenever a new limit is detected, so an API that tightens its quota mid-run is followed immediately.

## 3. Detection

After the response arrives, the detector looks for rate-limit headers in this order:

1. A **per-API profile**, if the host is one of `github.com`, `api.stripe.com`, `api.twitter.com`, `api.openai.com`
2. Your **custom `headers_map`**, if you passed one
3. **Standard patterns** — `X-RateLimit-*`, `RateLimit-*`, `X-Rate-Limit-*`, `X-RateLimit-Requests-*`
4. On a 429 only: **`Retry-After`**, which yields a window but no limit

Reset values are parsed as a Unix timestamp, an ISO 8601 datetime, or a relative number of seconds — values under 86400 are read as "seconds from now", larger ones as absolute timestamps. Details and the full header list: [Detection & Headers](detection.md).

If nothing is detected, nothing is stored, and requests to that endpoint stay unpaced unless you supplied `default_limits` or called `set_limit()`.

## 4. Storage

The stored quota and bucket live behind a small interface with three implementations — memory, SQLite, Redis — selected by the `storage=` string. Same behaviour, different blast radius:

| | Survives restart | Shared across processes | Shared across machines |
|---|---|---|---|
| `memory` | no | no | no |
| `sqlite:///file.db` | yes | yes, same machine | no |
| `redis://host:port/0` | yes | yes | yes |

Tokens are consumed **inside** the store as one atomic step — under the lock for
memory, in a `BEGIN IMMEDIATE` transaction for SQLite, in a Lua script for Redis
— so no two workers can spend the same token.

## 5. Scopes

A limit belongs to an **endpoint scope**: a host, optionally narrowed by a path
prefix. `https://api.example.com` covers the whole host;
`https://api.example.com/search` covers only paths under `/search`.

Resolving a request walks from the most specific scope to the least —
`/v1/users/42`, then `/v1/users`, then `/v1`, then the bare host — and uses the
first one with a stored limit. That is what lets a host-wide default and a
narrow override coexist without either knowing about the other, and it gives
each scope its own token bucket, so exhausting `/search` leaves `/users` alone.

Query strings and trailing slashes are dropped when matching: they identify a
request, not a quota. Detected limits attach to whichever scope is governing the
URL, but never overwrite one you set yourself — a `confidence="configured"`
entry stands, because you set it precisely when the headers could not be
trusted.

See [Which storage backend?](choosing.md) and [Storage Backends](storage.md).

!!! note "Failing open is the default, not the only option"
    An unreachable Redis fails *open*: the request goes out unpaced and a
    warning is logged, so a limiter outage does not take your job down with it.
    When the limit guards a paid quota that trade is wrong — pass
    `fail_closed=True` and the limiter raises `StorageUnavailable` (a subclass
    of `RateLimitExceeded`) instead of waving traffic through.

    ```python
    limiter = RateLimiter(storage="redis://localhost:6379/0", fail_closed=True)
    ```

    Either way it is logged. Redis is pinged at construction, because
    `redis-py` connects lazily and an unchecked client looks healthy right up
    until it silently stops limiting.

## 6. Dimensions

A scope can meter more than one thing. `requests` is universal; an LLM API also
meters `tokens`, and the token budget is usually the one that binds — a caller
well inside its requests-per-minute allowance still gets a 429 once
tokens-per-minute is spent.

Each dimension has its own budget and its own bucket. A request declares what it
spends:

```python
limiter.request("POST", url, json=payload, cost={"tokens": 1500})
```

and is admitted only when **every** dimension it touches can pay. The charge is
one atomic step across all of them, which is not a detail: charging them in turn
would let a request spend its request allowance and then be refused for tokens,
draining the wrong budget on every rejection. A refused request charges nothing.

A dimension a request does not spend never gates it, so a `GET` with no token
cost is not held up by an exhausted token budget.

## 7. Provider profiles

Detection needs a response. Some limits matter before you have one — GitHub
allows 60 requests an hour unauthenticated, and no response tells you whether
your credentials were accepted until you have already spent a request finding
out.

For a small set of hosts the library seeds documented limits at construction,
marked `confidence="registry"` and replaced as soon as real headers arrive. The
table is deliberately tiny: most providers meter per account tier, so a baked-in
number would be a guess about *your* account, and those providers send their
limits in headers anyway. See [Provider profiles](providers.md).

## What happens on a 429

Even with pacing you can still be handed a 429 — another client on the same key, a limit the API never advertised, a burst that started before the first response taught the limiter anything.

When `request()` sees a 429 (or a 503 or 504), it retries according to its `RetryConfig` — three attempts by default. A `Retry-After` header decides the wait, since the server knows when its window reopens; both the seconds form and the HTTP-date form are honoured, capped at `max_delay`. Without that header the limiter falls back to exponential backoff with jitter. If the last attempt still fails, the response is returned as-is for you to handle.

```python
from smartratelimit.retry import RetryConfig

limiter = RateLimiter(retry=RetryConfig(max_retries=5, max_delay=30.0))
```

Set `raise_on_limit=True` and the limiter never sleeps: it raises `RateLimitExceeded` when the bucket is empty, and raises rather than waiting out a retryable rejection. That's the right mode for a request handler where a slow response is worse than an error.

```python
from smartratelimit import RateLimiter, RateLimitExceeded

limiter = RateLimiter(raise_on_limit=True)

try:
    response = limiter.request("GET", "https://api.example.com/data")
except RateLimitExceeded as e:
    print(f"Would have waited: {e}")
```

## Threads and processes

`RateLimiter` is safe to share across threads — all three backends guard their state with a lock. Each `RateLimiter` also owns one `requests.Session`, which is thread-safe for ordinary use.

What a lock cannot do is coordinate *separate processes*. Two Gunicorn workers with `storage="memory"` each believe they own the whole quota, and together they spend it twice as fast. That is what the Redis backend exists for.

The sleeping is **blocking** — `time.sleep` on the calling thread. In a thread pool that's usually what you want. In an event loop it is not: use [`AsyncRateLimiter`](async.md), which awaits instead.
