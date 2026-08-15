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

!!! note "`storage=` takes a string, not an object"
    The constructor parses a connection string; it does not accept a backend
    instance. Assigning to `limiter._storage` after construction is the current
    way to install a pre-built backend, and `_storage` is private — it may
    change between releases. For the three standard backends, prefer the string.

### Verifying the connection

Construction falls back to memory on failure rather than raising, so check explicitly if shared state matters:

```python
from smartratelimit.storage import RedisStorage

try:
    RedisStorage("redis://localhost:6379/0").redis_client.ping()
    print("✓ Redis reachable — limits are shared")
except Exception as e:
    raise SystemExit(f"✗ Redis unavailable: {e}")
```

Redis operations at runtime also degrade quietly: a read that fails returns `None` (treated as "no limit known") and a write that fails is dropped. The job keeps going; the pacing gets less accurate.

## Writing your own backend

Subclass `StorageBackend` and implement five methods:

```python
from typing import Optional

from smartratelimit.storage import StorageBackend
from smartratelimit.models import RateLimit, TokenBucket


class DynamoStorage(StorageBackend):
    def get_rate_limit(self, endpoint: str) -> Optional[RateLimit]: ...
    def set_rate_limit(self, endpoint: str, rate_limit: RateLimit) -> None: ...
    def get_token_bucket(self, key: str) -> Optional[TokenBucket]: ...
    def set_token_bucket(self, key: str, bucket: TokenBucket) -> None: ...
    def clear(self, endpoint: Optional[str] = None) -> None: ...
```

Contract notes:

- `endpoint` keys are `scheme://host`; bucket keys are `scheme://host:default`
- Getters return `None` when nothing is stored — never raise for a miss
- `clear(endpoint)` must also remove that endpoint's bucket keys; `clear(None)` removes everything
- Assume concurrent calls from multiple threads

Then attach it:

```python
limiter = RateLimiter()
limiter._storage = DynamoStorage(...)
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
