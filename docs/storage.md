# Storage Backends

All three backends implement one interface and are selected with a connection string. If you haven't picked yet, start at [Which storage backend?](choosing.md).

## Memory

The default. A dict guarded by an `RLock`, with expired rate limits swept out at most once an hour.

```python
from smartratelimit import RateLimiter

limiter = RateLimiter()                    # same as storage="memory"
```

Two `RateLimiter()` instances in the same process do **not** share memory storage — each constructs its own. If you want two limiters (different header maps, say) to share state, give them a SQLite or Redis URL.

## SQLite

```python
limiter = RateLimiter(storage="sqlite:///ratelimit.db")
```

Two tables are created on first use: `rate_limits` (quota, reset time, window per endpoint) and `token_buckets` (capacity, level, refill rate per bucket key). Timestamps are stored as ISO strings.

### Path forms

```python
"sqlite:///ratelimit.db"               # relative to the working directory
"sqlite:///data/ratelimit.db"          # relative subdirectory
"sqlite:////var/lib/app/limits.db"     # absolute path — note four slashes
"sqlite:///:memory:"                   # in-process database, nothing on disk
```

The directory must already exist; SQLite creates the file, not the folder.

### Reading it from elsewhere

The file is an ordinary SQLite database, so anything can read it — a monitoring script, `sqlite3`, or the bundled CLI:

```bash
smartratelimit --storage "sqlite:///ratelimit.db" status "https://api.github.com"
```

That is the main reason to prefer SQLite over memory even for a single process: your job's quota becomes observable from outside the job.

### Sharing between processes

Several processes on one machine can open the same file and will genuinely share the quota:

```python
# worker.py — run this as many times as you like
from smartratelimit import RateLimiter

limiter = RateLimiter(storage="sqlite:///shared.db")
limiter.request("GET", "https://api.example.com/data")
```

Writes are serialised by SQLite's own locking, but reads and writes are not one transaction — two workers can read the same bucket level before either writes back. Sharing is approximate under high concurrency. For a handful of workers doing paced work it holds up well; for dozens, use Redis.

## Redis

```bash
pip install smartratelimit[redis]
```

```python
limiter = RateLimiter(storage="redis://localhost:6379/0")
```

State lives in Redis hashes under the `ratelimit:` key prefix:

```
ratelimit:rate_limit:https://api.github.com
ratelimit:token_bucket:https://api.github.com:default
```

Keys expire on their own — rate limits after their window plus an hour, buckets after 24 hours — so nothing accumulates.

### Connection strings

```python
"redis://localhost:6379/0"                        # default
"redis://:password@localhost:6379/0"              # password only
"redis://user:password@redis.internal:6379/1"     # ACL user, database 1
"rediss://user:password@redis.example.com:6380/0" # TLS
```

Any URL `redis-py`'s `from_url()` understands works, as long as it starts with `redis://` — note that a `rediss://` URL will **not** match the `redis://` prefix check in the constructor and raises `ValueError`. For TLS, build the backend yourself:

```python
from smartratelimit import RateLimiter
from smartratelimit.storage import RedisStorage

limiter = RateLimiter()
limiter._storage = RedisStorage("rediss://user:pass@redis.example.com:6380/0")
```

### Custom key prefix

Useful when several applications share one Redis database:

```python
from smartratelimit.storage import RedisStorage

limiter = RateLimiter()
limiter._storage = RedisStorage(
    redis_url="redis://localhost:6379/0",
    key_prefix="billing-worker:ratelimit:",
)
```

!!! note "`storage=` takes a string **or** a backend instance"
    The connection string covers the common cases. For options it cannot
    express — a custom Redis `key_prefix`, a backend of your own — pass a
    constructed `StorageBackend` instead:

    ```python
    from smartratelimit.storage import RedisStorage

    limiter = RateLimiter(
        storage=RedisStorage("redis://localhost:6379/0", key_prefix="myapp:")
    )
    ```

### When the backend is unreachable

`redis-py` connects lazily, so the limiter pings Redis at construction rather than discovering the problem on your first real request. What happens next is your call:

```python
# Default: fail open. Requests go out unpaced, loudly logged.
limiter = RateLimiter(storage="redis://localhost:6379/0")

# Fail closed. Raises StorageUnavailable rather than sending unpaced traffic.
limiter = RateLimiter(storage="redis://localhost:6379/0", fail_closed=True)
```

Failing open is right when the limit is advisory — a limiter outage should not take your job down with it. It is wrong when the limit guards a paid quota, where unpaced traffic is the more expensive failure. `StorageUnavailable` subclasses `RateLimitExceeded`, so handlers you already have keep working.

The client is kept either way rather than being swapped for memory storage, so a Redis that is briefly down at boot resumes sharing limits when it returns. Runtime read and write failures are logged: a failed read is treated as "no limit known", a failed write is dropped, and a failed `acquire` either fails open with a warning or raises, per `fail_closed`. Nothing is silent.

## Writing your own backend

Subclass `StorageBackend` and implement seven methods:

```python
from typing import List, Optional, Tuple

from smartratelimit.storage import StorageBackend
from smartratelimit.models import RateLimit, TokenBucket


class DynamoStorage(StorageBackend):
    def get_rate_limit(self, endpoint: str) -> Optional[RateLimit]: ...
    def set_rate_limit(self, endpoint: str, rate_limit: RateLimit) -> None: ...
    def get_token_bucket(self, key: str) -> Optional[TokenBucket]: ...
    def set_token_bucket(self, key: str, bucket: TokenBucket) -> None: ...
    def clear(self, endpoint: Optional[str] = None) -> None: ...
    def list_endpoints(self) -> List[str]: ...

    def acquire(
        self,
        key: str,
        capacity: float,
        refill_rate: float,
        tokens: float = 1.0,
    ) -> Tuple[bool, float]: ...
```

**`acquire` is the one that matters.** It must refill the bucket and consume from it as a single indivisible operation, against however many callers your backend can have at once. Building it out of `get_token_bucket` / mutate / `set_token_bucket` is not a substitute: two callers read the same token count, both write it back one lower, and two requests cost one token. Use whatever your store gives you — a transaction, a compare-and-swap, a server-side script.

Return `(True, 0.0)` when the tokens were consumed, and `(False, seconds_to_wait)` when they were not — with nothing consumed. Return `float("inf")` as the wait if the bucket can never refill.

Contract notes:

- `endpoint` keys are endpoint scopes: `scheme://host`, optionally with a path prefix (`https://api.example.com/search`). Bucket keys are that scope plus `:default`.
- Getters return `None` when nothing is stored — never raise for a miss
- `clear(endpoint)` must also remove that endpoint's bucket keys; `clear(None)` removes everything
- `list_endpoints()` returns every scope with a stored limit; the CLI's `list` reads it
- Optionally override `get_rate_limit_for(candidates)` to resolve a scope in one round trip. The default walks the list calling `get_rate_limit`, which costs a query per path segment on a remote store. Candidates arrive most-specific-first and that order is the precedence order.
- Assume concurrent calls from multiple threads, and — if your store is shared — from multiple processes

Then pass it in:

```python
limiter = RateLimiter(storage=DynamoStorage(...))
```

## Clearing state

```python
limiter.clear("api.github.com")   # one endpoint, quota and bucket
limiter.clear()                   # everything in this backend
```

Or from the shell, against the same backend:

```bash
smartratelimit --storage "sqlite:///ratelimit.db" clear "https://api.github.com"
smartratelimit --storage "sqlite:///ratelimit.db" clear
```

Clearing makes the limiter forget a quota entirely — the next request goes out unpaced and re-learns from the response headers. That is usually what you want after changing an API key or plan tier.
