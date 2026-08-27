# Provider Profiles

Detection needs a response. Some limits matter *before* you have one.

GitHub allows **60 requests an hour** to an unauthenticated caller. Learning that
from the first response has already spent a meaningful slice of the budget — and
nothing in a response tells you whether your credentials were accepted until
you have sent one.

So for a small set of hosts, smartratelimit seeds documented limits up front.

```python
limiter = RateLimiter()                              # GitHub seeded at 60/hour
limiter = RateLimiter(authenticated=True)            # GitHub seeded at 5000/hour
limiter = RateLimiter(use_provider_profiles=False)   # no seeding
```

`authenticated` is a statement about your client, not a detection: the anonymous
and authenticated ceilings differ by nearly two orders of magnitude, and the
limiter cannot tell which applies to you.

## Seeds are placeholders

Everything seeded is stored with `confidence="registry"` and is **replaced the
moment the API reports its own numbers**.

```python
limiter.get_status("api.github.com").confidence
# 'registry'  -> before the first response
# 'confirmed' -> after
```

A limit you set with `set_limit()` is `configured` and outranks both: seeds never
overwrite it.

## Why the table is tiny

The obvious version of this feature is a big table of every popular API's limits.
That table would be wrong more often than right.

**Most providers meter per account tier.** OpenAI's requests-per-minute depends
on which tier *you* are on, not on OpenAI. A baked-in number is a guess about
your account that this library is in no position to make — and a wrong baked-in
limit is worse than none, because it either throttles you against a ceiling that
does not exist or lets you sail past the real one, with nothing to tell you
either way.

**Those providers already send their limits in headers**, which detection reads.
A registry entry would replace live, account-correct data with a stale copy.

An entry earns its place only when both hold:

1. The number cannot be detected in time to be useful — the API sends no
   rate-limit headers, or you need it before the first response.
2. The number is a published property of the API itself, not of your account.

OpenAI, Anthropic and Stripe are all deliberately **absent** for this reason.
Their token and request limits are detected from headers instead — see
[Detection & Headers](detection.md).

## What ships

| Host | Scope | Anonymous | Authenticated |
|---|---|---|---|
| `api.github.com` | host | 60 / hour | 5000 / hour |
| `api.github.com` | `/search` | 10 / minute | 30 / minute |

## Registering your own

This is the case the mechanism is really for: an internal service that documents
a limit in a runbook and sends no headers.

```python
from smartratelimit import Provider, SeedLimit, register_provider

register_provider("internal.api", Provider(
    name="Internal service",
    reason="Sends no rate-limit headers; limit is in the runbook.",
    scopes={
        "": [SeedLimit(25, "1m")],
        "/bulk": [SeedLimit(2, "1m")],
    },
))
```

`reason` is required by convention rather than by the type system — it is there
so whoever reads the entry in a year can tell whether it still earns its place.

Seeds can cover [multiple dimensions](concepts.md#6-dimensions) too:

```python
register_provider("llm.internal", Provider(
    name="Internal LLM gateway",
    reason="No headers; limits published on the platform wiki.",
    scopes={"": [
        SeedLimit(60, "1m"),
        SeedLimit(50_000, "1m", dimension="tokens"),
    ]},
))
```

Scope suffixes follow the same rules as [`set_limit`](api.md#set_limitendpoint-limit-window1h---none):
`""` is the host, `"/search"` narrows to that path prefix and takes precedence
there.
