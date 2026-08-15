# Metrics & Monitoring

`MetricsCollector` counts what happened per endpoint and exports it as Prometheus text or JSON.

It is **not automatic** — the limiter doesn't know about the collector. You record each request yourself, which is a line of code and buys you the freedom to label endpoints however your dashboards want.

```python
from smartratelimit import RateLimiter
from smartratelimit.metrics import MetricsCollector

limiter = RateLimiter()
metrics = MetricsCollector()

response = limiter.request("GET", "https://api.github.com/users/octocat")
metrics.record_request(
    "api.github.com",
    response.status_code,
    limiter.get_status("api.github.com"),
)
```

## What gets tracked

Per endpoint key:

| Field | Meaning |
|---|---|
| `total_requests` | Every recorded call |
| `successful_requests` | Status 2xx |
| `rate_limited_requests` | Status 429 |
| `other_errors` | Everything else (3xx, 4xx except 429, 5xx) |
| `rate_limit_history` | Last 100 snapshots of limit / remaining / utilization / reset_in |

The history is capped at 100 entries per endpoint, so a long-running process won't grow without bound.

Passing the `RateLimitStatus` is optional — omit it and you still get the counters, just no history or gauges.

## A recording helper

Rather than repeating three lines at every call site:

```python
from smartratelimit import RateLimiter
from smartratelimit.metrics import MetricsCollector

limiter = RateLimiter(storage="sqlite:///ratelimit.db")
metrics = MetricsCollector()


def call(method, url, **kwargs):
    response = limiter.request(method, url, **kwargs)
    endpoint = url.split("/")[2]                      # host
    metrics.record_request(endpoint, response.status_code, limiter.get_status(endpoint))
    return response


call("GET", "https://api.github.com/users/octocat")
call("GET", "https://api.agify.io", params={"name": "Sarah"})
```

Keep the endpoint label **low-cardinality** — a host, or a host plus a route family. Never a label containing user IDs or query strings; Prometheus will punish you for it.

## Prometheus export

```python
print(metrics.export_prometheus())
```

```
# HELP ratelimit_total_requests Total number of requests
# TYPE ratelimit_total_requests counter
# HELP ratelimit_successful_requests Number of successful requests
# TYPE ratelimit_successful_requests counter
...
ratelimit_total_requests{endpoint="api.github.com"} 42
ratelimit_successful_requests{endpoint="api.github.com"} 40
ratelimit_rate_limited_requests{endpoint="api.github.com"} 2
ratelimit_other_errors{endpoint="api.github.com"} 0
ratelimit_remaining{endpoint="api.github.com"} 4958
ratelimit_limit{endpoint="api.github.com"} 5000
ratelimit_utilization{endpoint="api.github.com"} 0.0084
```

The three gauges (`remaining`, `limit`, `utilization`) come from the most recent history entry, so they only appear for endpoints you recorded a status for.

### Serving it

Any HTTP handler will do. FastAPI:

```python
from fastapi import FastAPI, Response

app = FastAPI()


@app.get("/metrics")
def prometheus_metrics():
    return Response(metrics.export_prometheus(), media_type="text/plain")
```

Flask:

```python
@app.route("/metrics")
def prometheus_metrics():
    return metrics.export_prometheus(), 200, {"Content-Type": "text/plain"}
```

!!! note "One collector per process"
    `MetricsCollector` keeps its counters in memory, so each worker exports its
    own. That is the normal Prometheus model — scrape every worker and
    aggregate with `sum by (endpoint)` — but it does mean the numbers are
    per-process even when the *rate limit* is shared via Redis.

## JSON export

For logs, a debug endpoint, or a quick look:

```python
print(metrics.export_json())
```

```json
{
  "api.github.com": {
    "total_requests": 42,
    "successful_requests": 40,
    "rate_limited_requests": 2,
    "other_errors": 0,
    "rate_limit_history": [
      {
        "timestamp": "2026-08-15T10:31:04.220981",
        "limit": 5000,
        "remaining": 4958,
        "utilization": 0.0084,
        "reset_in": 2841.7
      }
    ]
  }
}
```

## Reading counters in code

```python
all_metrics = metrics.get_metrics()                       # every endpoint
github = metrics.get_metrics("api.github.com")            # one endpoint, {} if unknown

if github and github["total_requests"]:
    ratio = github["rate_limited_requests"] / github["total_requests"]
    if ratio > 0.05:
        logger.warning("Over 5%% of GitHub calls are being rate limited")
```

## Resetting

```python
metrics.reset("api.github.com")   # one endpoint
metrics.reset()                   # all of them
```

Useful between test cases, or at the start of each batch run when you report per-run numbers.

## Alerts worth having

Three that consistently earn their keep:

```promql
# Rate limiting is actually happening
rate(ratelimit_rate_limited_requests[5m]) > 0

# Burning quota faster than the window can refill it
ratelimit_utilization > 0.9

# Quota nearly gone with a long way to the reset
ratelimit_remaining < 10
```

The second is the early warning — utilization above 0.9 means the next burst turns into waiting, which shows up as latency long before it shows up as errors.
