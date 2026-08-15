<div class="ov-hero">
  <div class="ov-hero-badge">v{{ smartratelimit_version }}</div>
  <h1 class="ov-hero-title">smartratelimit</h1>
  <p class="ov-hero-sub">Your API's rate limits, read from its own headers and respected automatically — no 429s, no hand-rolled sleep loops</p>
  <div class="ov-hero-install">
    <span class="ov-hero-install-label">pip install smartratelimit</span>
  </div>
  <div class="ov-hero-links">
    <a href="quickstart/" class="md-button md-button--primary">Quick Start</a>
    <a href="https://github.com/Olaverse-Labs/smartratelimit" class="md-button" target="_blank">GitHub</a>
    <a href="https://pypi.org/project/smartratelimit/" class="md-button" target="_blank">PyPI</a>
  </div>
</div>

---

## What is smartratelimit?

Every API tells you its rate limit on every response — `X-RateLimit-Limit`, `X-RateLimit-Remaining`, `X-RateLimit-Reset` — and almost no client reads them. So you either guess a `time.sleep()`, or you find out the hard way with a 429.

`smartratelimit` reads those headers for you. Swap `requests.get(url)` for `limiter.request('GET', url)` and the limiter **learns the limit from the first response**, refills a token bucket at exactly that rate, and blocks the calling thread just long enough when you're about to run out. Nothing to configure — most APIs are handled by the headers they already send.

When you need more than one process to share those limits — Gunicorn workers, Celery tasks, a fleet of scrapers — point it at SQLite or Redis and the token bucket becomes shared state.

**Start here:** [60-second quickstart](quickstart/) · [How it works](concepts/) · [Which storage backend?](choosing/)

---

## What you get

<div class="ov-grid">

<div class="ov-card">
  <div class="ov-card-icon">📡</div>
  <div class="ov-card-title">Automatic detection</div>
  <div class="ov-card-body">Reads <code>X-RateLimit-*</code>, <code>RateLimit-*</code> and <code>Retry-After</code> headers, with built-in profiles for GitHub, Stripe, Twitter, OpenAI and RapidAPI. Non-standard header names take a three-line map.</div>
  <a href="detection/" class="ov-card-link">Explore Detection →</a>
</div>

<div class="ov-card">
  <div class="ov-card-icon">🪣</div>
  <div class="ov-card-title">Token bucket pacing</div>
  <div class="ov-card-body">The detected limit becomes a bucket that refills at <code>limit / window</code> per second. Requests are spread across the window instead of sprinting into the wall and stalling.</div>
  <a href="concepts/" class="ov-card-link">How it works →</a>
</div>

<div class="ov-card">
  <div class="ov-card-icon">💾</div>
  <div class="ov-card-title">Persistent state</div>
  <div class="ov-card-body">Memory, SQLite, or Redis behind one <code>storage=</code> string. Restart your app and it still knows it has 12 requests left on this hour's quota.</div>
  <a href="storage/" class="ov-card-link">Explore Storage →</a>
</div>

<div class="ov-card">
  <div class="ov-card-icon">🔀</div>
  <div class="ov-card-title">Multi-process safe</div>
  <div class="ov-card-body">With Redis, every worker draws from the same bucket — the limit is per API, not per process. Works with Gunicorn, Celery, and multi-machine deployments.</div>
  <a href="choosing/" class="ov-card-link">Compare backends →</a>
</div>

<div class="ov-card">
  <div class="ov-card-icon">⚡</div>
  <div class="ov-card-title">Async support</div>
  <div class="ov-card-body"><code>AsyncRateLimiter</code> paces <code>httpx</code> and <code>aiohttp</code> calls with <code>asyncio.sleep</code>, so waiting on one endpoint never blocks the loop.</div>
  <a href="async/" class="ov-card-link">Explore Async →</a>
</div>

<div class="ov-card">
  <div class="ov-card-icon">🔄</div>
  <div class="ov-card-title">Retry strategies</div>
  <div class="ov-card-body">Exponential, linear, or fixed backoff over any callable, sync or async, with a capped delay and a configurable list of retryable status codes.</div>
  <a href="retry/" class="ov-card-link">Explore Retry →</a>
</div>

<div class="ov-card">
  <div class="ov-card-icon">📊</div>
  <div class="ov-card-title">Metrics</div>
  <div class="ov-card-body">Count requests, 429s and utilization per endpoint, then export Prometheus text or JSON straight into your existing scrape target.</div>
  <a href="metrics/" class="ov-card-link">Explore Metrics →</a>
</div>

<div class="ov-card">
  <div class="ov-card-icon">🖥️</div>
  <div class="ov-card-title">CLI</div>
  <div class="ov-card-body"><code>smartratelimit probe</code> shows what an API advertises, <code>status</code> reads the stored quota, <code>clear</code> resets it — useful against a shared SQLite or Redis backend.</div>
  <a href="cli/" class="ov-card-link">Explore CLI →</a>
</div>

</div>

---

## Sixty seconds

```python
from smartratelimit import RateLimiter

limiter = RateLimiter()

# The first response teaches the limiter GitHub's limit; every call after
# that is paced to fit inside it.
for user in ["octocat", "torvalds", "gvanrossum"]:
    response = limiter.request("GET", f"https://api.github.com/users/{user}")
    print(response.json()["name"])

status = limiter.get_status("api.github.com")
print(f"{status.remaining}/{status.limit} left, resets in {status.reset_in:.0f}s")
```

Nothing above names a rate limit. GitHub's headers did.

---

## When you don't need it

If you call an API a handful of times a day, a bare `requests.get` is fine. `smartratelimit` earns its place when you're **looping** over an API — batch enrichment, scraping, fan-out jobs, sync workers — where the limit is a real constraint and the failure mode is a 429 partway through a run.

It also assumes the limit belongs to *you calling someone else*. It is a client-side limiter, not a server-side one; it won't throttle inbound traffic to your own service.

---

## Installation

```bash
pip install smartratelimit            # core, requests only
pip install smartratelimit[redis]     # + Redis storage backend
pip install smartratelimit[httpx]     # + httpx async client
pip install smartratelimit[aiohttp]   # + aiohttp async client
pip install smartratelimit[all]       # everything
```

Python 3.8+. The only hard dependency is `requests`.
