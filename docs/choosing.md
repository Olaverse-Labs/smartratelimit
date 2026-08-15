# Which storage backend?

The `storage=` string is the one decision that matters at setup. Everything else has a working default.

## Pick by deployment shape

<div class="ov-compare-grid">

<div class="ov-compare-card">
  <div class="ov-compare-badge ov-badge-green">memory</div>
  <div class="ov-compare-title">One process, nothing to keep</div>
  <ul>
    <li>Scripts, notebooks, tests</li>
    <li>A single long-running worker</li>
    <li>No files, no services, no setup</li>
    <li>State dies with the process</li>
  </ul>
</div>

<div class="ov-compare-card">
  <div class="ov-compare-badge ov-badge-purple">sqlite / redis</div>
  <div class="ov-compare-title">Restarts or multiple workers</div>
  <ul>
    <li>SQLite: one machine, quota survives restarts</li>
    <li>Redis: many workers or many machines, one shared quota</li>
    <li>Cron jobs that must not re-spend the quota</li>
    <li>Anything behind Gunicorn, Celery, or a job queue</li>
  </ul>
</div>

</div>

## The full comparison

| | `memory` | `sqlite:///file.db` | `redis://host:6379/0` |
|---|---|---|---|
| Survives restart | ❌ | ✅ | ✅ |
| Shared across processes | ❌ | ✅ same machine | ✅ |
| Shared across machines | ❌ | ❌ | ✅ |
| External service | none | none | Redis server |
| Extra install | none | none | `pip install smartratelimit[redis]` |
| Per-call cost | in-process dict | file write + fsync | one network round trip |
| Concurrency model | `RLock` | `RLock` + SQLite locking | `RLock` + Redis |

## Choosing in one question

> **Can two things be calling this API at the same time, or after a restart?**

- **No** → `memory`. Don't pay for durability you won't read.
- **Yes, on one machine** → `sqlite:///ratelimit.db`. Zero setup, and the CLI can read the same file.
- **Yes, across machines** → `redis://...`. The only backend where "the limit" genuinely means one limit.

## Things worth knowing before you commit

**SQLite writes on every request.** Each request reads and writes rows and commits. For a job doing a few requests per second that's invisible; for a tight loop against a very high quota it isn't free. If you're paced at 1000 requests/hour anyway, this never matters.

**SQLite across processes is real but coarse.** Multiple processes on one machine do share the file, and updates are serialised by SQLite's own locking. Under heavy simultaneous writes you can see lock contention, and two workers can each read the same bucket state before either writes — the shared quota is approximate, not transactional.

**Redis is shared but not atomic.** State lives in Redis hashes with a read-modify-write in the client, not a Lua script. Many workers still drift slightly under high concurrency. It is dramatically better than each worker guessing alone, and it is not a distributed lock.

**Redis keys expire on their own.** Rate limits get a TTL of their window plus an hour; token buckets get 24 hours. Nothing to clean up.

**Both fail soft.** An unreachable Redis or an unwritable SQLite path logs a warning and falls back to memory. Check explicitly at startup if shared state is load-bearing — see the note in [How it works](concepts.md#4-storage).

## Connection strings

```python
RateLimiter(storage="memory")                                  # default

RateLimiter(storage="sqlite:///ratelimit.db")                  # relative path
RateLimiter(storage="sqlite:////var/lib/app/ratelimit.db")     # absolute path (4 slashes)
RateLimiter(storage="sqlite:///:memory:")                      # in-process SQLite

RateLimiter(storage="redis://localhost:6379/0")
RateLimiter(storage="redis://:password@localhost:6379/0")
RateLimiter(storage="redis://user:password@redis.internal:6379/1")
```

Anything else raises `ValueError`. Working with a backend directly — a custom key prefix, a pre-built client — is covered in [Storage Backends](storage.md).
