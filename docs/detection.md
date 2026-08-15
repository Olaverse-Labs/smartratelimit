# Detection & Headers

Detection is the reason `RateLimiter()` needs no arguments. This page is what it looks for, in what order, and what to do when an API doesn't play along.

## Resolution order

For every response, the detector tries these in sequence and stops at the first that yields a usable limit:

1. **Per-API profile** — matched on the response host
2. **Your `headers_map`** — if you passed one to the constructor
3. **Standard header patterns** — the common conventions
4. **`Retry-After`** — 429 responses only

## 1. Per-API profiles

| Host | Limit | Remaining | Reset |
|---|---|---|---|
| `github.com` | `X-RateLimit-Limit` | `X-RateLimit-Remaining` | `X-RateLimit-Reset` |
| `api.stripe.com` | `Stripe-RateLimit-Limit` | `Stripe-RateLimit-Remaining` | `Stripe-RateLimit-Reset` |
| `api.twitter.com` | `x-rate-limit-limit` | `x-rate-limit-remaining` | `x-rate-limit-reset` |
| `api.openai.com` | `x-ratelimit-limit-requests` | `x-ratelimit-remaining-requests` | `x-ratelimit-reset-requests` |

Profiles are matched against the exact host of the response URL. `api.github.com` is not `github.com` — requests to the API host fall through to the standard patterns in step 3, which recognise the same `X-RateLimit-*` headers anyway. The profiles exist for APIs whose header names are *not* standard.

## 2. Custom header map

For an API with its own naming:

```python
from smartratelimit import RateLimiter

limiter = RateLimiter(
    headers_map={
        "limit":     "X-MyAPI-Quota",
        "remaining": "X-MyAPI-Quota-Left",
        "reset":     "X-MyAPI-Quota-Reset",
    }
)
```

The map applies to **every** host this limiter talks to, and it is consulted after the built-in profiles. `limit` is required for the map to match; `remaining` and `reset` are optional and filled in with defaults if missing (see [Gaps and fallbacks](#gaps-and-fallbacks)).

If you talk to several APIs with different conventions, use one limiter per API rather than one map that tries to cover both:

```python
github = RateLimiter(storage="sqlite:///limits.db")
vendor = RateLimiter(storage="sqlite:///limits.db",
                     headers_map={"limit": "X-Vendor-Limit",
                                  "remaining": "X-Vendor-Remaining",
                                  "reset": "X-Vendor-Reset"})
```

They can share a backend safely — state is keyed by endpoint.

## 3. Standard patterns

Recognised without any configuration, checked in this order:

| Role | Headers |
|---|---|
| Limit | `X-RateLimit-Limit`, `X-RateLimit-Requests-Limit`, `RateLimit-Limit`, `X-Rate-Limit-Limit` |
| Remaining | `X-RateLimit-Remaining`, `X-RateLimit-Requests-Remaining`, `RateLimit-Remaining`, `X-Rate-Limit-Remaining` |
| Reset | `X-RateLimit-Reset`, `X-RateLimit-Requests-Reset`, `RateLimit-Reset`, `X-Rate-Limit-Reset` |
| Retry after | `Retry-After`, `X-Retry-After` |

The `X-RateLimit-Requests-*` triplet is the RapidAPI convention, so most RapidAPI-hosted endpoints are detected out of the box.

Header lookup goes through `requests`' case-insensitive header mapping, so `x-ratelimit-limit` and `X-RateLimit-Limit` are the same header.

## 4. `Retry-After` on 429

If a 429 arrives with no rate-limit headers at all, `Retry-After` is still useful: it gives a window (and `remaining = 0`) even though it never gives a limit. Both integer seconds and RFC 7231 HTTP dates are parsed.

This path informs the *stored window*, and `request()` separately honours `Retry-After` by sleeping and retrying once. See [What happens on a 429](concepts.md#what-happens-on-a-429).

## How reset values are parsed

The reset header is tried as, in order:

| Value looks like | Read as | Example |
|---|---|---|
| An integer **< 86400** | Seconds from now | `3600` → resets in one hour |
| A number **> 1000000000** | Unix timestamp (UTC) | `1734567890` → an absolute time |
| ISO 8601 | Absolute datetime (`Z` accepted) | `2026-08-15T12:00:00Z` |
| Anything else numeric | Seconds from now | — |

The 86400 cutoff is the one rule to remember: a reset expressed as "seconds remaining" is only distinguishable from a timestamp by magnitude, so anything under a day is treated as relative.

Timestamps that resolve to a time in the past are discarded, and the window falls back to one hour.

## Gaps and fallbacks

APIs are inconsistent. What happens when a piece is missing:

| Missing | Behaviour |
|---|---|
| `remaining` | Assumed equal to `limit` — treated as a fresh window |
| `reset` | Window defaults to **1 hour** from now |
| `limit` | Nothing is stored; detection fails and the request is unpaced |

That last row is the important one. **Without a limit header there is nothing to pace against** — `remaining` alone isn't enough. Supply the limit yourself:

```python
# Fallback for any endpoint with no detected limit
limiter = RateLimiter(default_limits={"requests_per_minute": 60})

# Or state it per endpoint
limiter.set_limit("api.example.com", limit=5000, window="1h")
```

A detected limit always overrides `default_limits`; the defaults are only applied when nothing is stored for the endpoint yet.

## Finding out what an API sends

Before writing a header map, ask:

```bash
smartratelimit probe "https://api.github.com/users/octocat"
```

```
Probing https://api.github.com/users/octocat...

Response Status: 200

Rate Limit Headers:
  X-RateLimit-Limit: 60
  X-RateLimit-Remaining: 59
  X-RateLimit-Reset: 1734567890

Detected Rate Limit:
  Limit: 60
  Remaining: 59
  Window: 0:59:31
```

If the "Detected Rate Limit" section says nothing was found but you can see limit-ish headers in the response, that's exactly the case `headers_map` is for. More on the command: [CLI](cli.md#probe).

## Inspecting the detector directly

Useful in tests, or to check a map against a saved response:

```python
import requests
from smartratelimit.detector import RateLimitDetector

detector = RateLimitDetector({"limit": "X-MyAPI-Quota",
                              "remaining": "X-MyAPI-Quota-Left",
                              "reset": "X-MyAPI-Quota-Reset"})

response = requests.get("https://api.example.com/data")
print(detector.detect_from_response(response))
# {'limit': 1000, 'remaining': 998, 'reset_time': datetime(...), 'window': timedelta(...)}
# or None if nothing matched
```
