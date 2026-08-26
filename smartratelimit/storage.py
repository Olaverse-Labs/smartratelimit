"""Storage backends for rate limit state."""

import logging
import sqlite3
import threading
from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

from smartratelimit._time import to_epoch as _to_epoch
from smartratelimit._time import utcfromtimestamp as _from_epoch
from smartratelimit._time import utcnow
from smartratelimit.models import RateLimit, TokenBucket

logger = logging.getLogger(__name__)


def _covers(scope: str, key: str) -> bool:
    """
    Whether clearing ``scope`` should also remove ``key``.

    Clearing a host clears the path scopes under it, and their buckets. The
    boundary character matters: a bare ``startswith`` would let
    ``https://api.example.com`` match ``https://api.example.com.evil.com``.
    """
    return key == scope or key.startswith(scope + "/") or key.startswith(scope + ":")


class StorageBackend(ABC):
    """Abstract base class for storage backends."""

    @abstractmethod
    def get_rate_limit(self, endpoint: str) -> Optional[RateLimit]:
        """Get rate limit for an endpoint."""
        pass

    @abstractmethod
    def set_rate_limit(self, endpoint: str, rate_limit: RateLimit) -> None:
        """Store rate limit for an endpoint."""
        pass

    @abstractmethod
    def get_token_bucket(self, key: str) -> Optional[TokenBucket]:
        """Get token bucket for a key."""
        pass

    @abstractmethod
    def set_token_bucket(self, key: str, bucket: TokenBucket) -> None:
        """Store token bucket for a key."""
        pass

    @abstractmethod
    def clear(self, endpoint: Optional[str] = None) -> None:
        """Clear stored data for endpoint or all data."""
        pass

    @abstractmethod
    def list_endpoints(self) -> List[str]:
        """List every endpoint key that currently has a stored rate limit."""
        pass

    def get_rate_limit_for(self, candidates: List[str]) -> Optional[RateLimit]:
        """
        Return the stored limit for the first candidate that has one.

        Callers pass endpoint scopes most-specific-first, so a rule on
        ``https://host/search`` is found before the host-wide one. Backends
        override this to answer in a single round trip; the default walks the
        list, which is right for in-process storage.

        Args:
            candidates: Scope keys, most specific first.

        Returns:
            The matching :class:`RateLimit`, or None if none is stored.
        """
        for key in candidates:
            rate_limit = self.get_rate_limit(key)
            if rate_limit is not None:
                return rate_limit
        return None

    @abstractmethod
    def acquire(
        self,
        key: str,
        capacity: float,
        refill_rate: float,
        tokens: float = 1.0,
    ) -> Tuple[bool, float]:
        """
        Atomically refill a token bucket and try to consume from it.

        This is the operation the limiter actually relies on. Backends must
        implement it so that concurrent callers -- threads for memory storage,
        processes and hosts for SQLite and Redis -- cannot both observe the same
        token and consume it twice. A ``get`` / mutate / ``set`` sequence built
        on the accessors above is *not* a substitute: it loses updates under
        concurrency.

        Args:
            key: Bucket key.
            capacity: Maximum number of tokens the bucket holds.
            refill_rate: Tokens replenished per second.
            tokens: Tokens to consume.

        Returns:
            ``(allowed, wait_time)``. When ``allowed`` is True the tokens have
            been consumed and ``wait_time`` is 0.0. When False nothing was
            consumed and ``wait_time`` is the seconds to wait before enough
            tokens exist (``float('inf')`` if the bucket never refills).
        """
        pass


class MemoryStorage(StorageBackend):
    """In-memory storage backend with automatic cleanup."""

    def __init__(self, cleanup_interval: int = 3600):
        """
        Initialize in-memory storage.

        Args:
            cleanup_interval: Seconds between cleanup of expired entries
        """
        self._rate_limits: Dict[str, RateLimit] = {}
        self._token_buckets: Dict[str, TokenBucket] = {}
        self._lock = threading.RLock()
        self._cleanup_interval = cleanup_interval
        self._last_cleanup = utcnow()

    def _get_endpoint_key(self, url: str) -> str:
        """Extract endpoint key from URL."""
        parsed = urlparse(url)
        return f"{parsed.scheme}://{parsed.netloc}"

    def _cleanup_expired(self) -> None:
        """Remove expired rate limit entries."""
        now = utcnow()
        if (now - self._last_cleanup).total_seconds() < self._cleanup_interval:
            return

        # A rate limit with no reset time never expires on its own -- skip it
        # rather than raising midway through an unrelated store or fetch.
        expired_keys = [
            key for key, rate_limit in self._rate_limits.items()
            if rate_limit.reset_time is not None and rate_limit.reset_time < now
        ]

        for key in expired_keys:
            self._rate_limits.pop(key, None)

        self._last_cleanup = now

    def get_rate_limit(self, endpoint: str) -> Optional[RateLimit]:
        """Get rate limit for an endpoint."""
        with self._lock:
            self._cleanup_expired()
            return self._rate_limits.get(endpoint)

    def set_rate_limit(self, endpoint: str, rate_limit: RateLimit) -> None:
        """Store rate limit for an endpoint."""
        with self._lock:
            self._rate_limits[endpoint] = rate_limit
            self._cleanup_expired()

    def get_token_bucket(self, key: str) -> Optional[TokenBucket]:
        """Get token bucket for a key."""
        with self._lock:
            return self._token_buckets.get(key)

    def set_token_bucket(self, key: str, bucket: TokenBucket) -> None:
        """Store token bucket for a key."""
        with self._lock:
            self._token_buckets[key] = bucket

    def clear(self, endpoint: Optional[str] = None) -> None:
        """Clear stored data for endpoint or all data."""
        with self._lock:
            if endpoint:
                for key in [k for k in self._rate_limits if _covers(endpoint, k)]:
                    del self._rate_limits[key]
                for key in [k for k in self._token_buckets if _covers(endpoint, k)]:
                    del self._token_buckets[key]
            else:
                self._rate_limits.clear()
                self._token_buckets.clear()

    def list_endpoints(self) -> List[str]:
        """List every endpoint key that currently has a stored rate limit."""
        with self._lock:
            return list(self._rate_limits.keys())

    def acquire(
        self,
        key: str,
        capacity: float,
        refill_rate: float,
        tokens: float = 1.0,
    ) -> Tuple[bool, float]:
        """Refill and consume under the storage lock, so threads cannot race."""
        with self._lock:
            bucket = self._token_buckets.get(key)
            if bucket is None:
                bucket = TokenBucket(
                    capacity=capacity, tokens=capacity, refill_rate=refill_rate
                )
                self._token_buckets[key] = bucket
            else:
                bucket.capacity = capacity
                bucket.refill_rate = refill_rate
                bucket.tokens = min(bucket.tokens, capacity)

            if bucket.consume(tokens):
                return True, 0.0
            return False, bucket.wait_time(tokens)


class SQLiteStorage(StorageBackend):
    """SQLite-based persistent storage backend."""

    #: Seconds a writer waits for another process to release the database lock
    #: before giving up. Multi-process ``acquire`` serialises on that lock, so
    #: this needs to comfortably exceed the time one bucket update takes.
    BUSY_TIMEOUT_MS = 5000

    def __init__(self, db_path: str = ":memory:"):
        """
        Initialize SQLite storage.

        Args:
            db_path: Path to SQLite database file, or ":memory:" for in-memory DB
        """
        self.db_path = db_path
        self._lock = threading.RLock()
        # For in-memory databases, we need to keep a connection open
        if db_path == ":memory:":
            self._conn = self._connect()
            self._init_db(self._conn)
        else:
            self._conn = None
            self._init_db()

    def _connect(self) -> sqlite3.Connection:
        """Open a connection configured for multi-process token accounting."""
        conn = sqlite3.connect(
            self.db_path,
            check_same_thread=False,
            timeout=self.BUSY_TIMEOUT_MS / 1000.0,
        )
        # Autocommit: transactions are opened explicitly so that ``acquire``
        # can hold a write lock across its read-modify-write.
        conn.isolation_level = None
        conn.execute(f"PRAGMA busy_timeout = {self.BUSY_TIMEOUT_MS}")
        if self.db_path != ":memory:":
            # WAL lets readers proceed while a writer holds the lock, which
            # keeps concurrent limiters from serialising on reads too.
            try:
                conn.execute("PRAGMA journal_mode = WAL")
            except sqlite3.DatabaseError:  # pragma: no cover - exotic filesystems
                pass
        return conn

    def _init_db(self, conn: Optional[sqlite3.Connection] = None) -> None:
        """Initialize database tables."""
        if conn is None:
            conn = self._connect()
            close_conn = True
        else:
            close_conn = False

        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS rate_limits (
                    endpoint TEXT PRIMARY KEY,
                    limit_value INTEGER NOT NULL,
                    remaining INTEGER NOT NULL,
                    reset_time TEXT NOT NULL,
                    window_seconds REAL NOT NULL,
                    last_updated TEXT NOT NULL,
                    confidence TEXT NOT NULL DEFAULT 'confirmed'
                )
            """
            )
            # Databases written before confidence tracking existed keep working;
            # their rows read back as 'confirmed'.
            columns = {
                row[1] for row in conn.execute("PRAGMA table_info(rate_limits)")
            }
            if "confidence" not in columns:
                conn.execute(
                    "ALTER TABLE rate_limits "
                    "ADD COLUMN confidence TEXT NOT NULL DEFAULT 'confirmed'"
                )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS token_buckets (
                    key TEXT PRIMARY KEY,
                    capacity REAL NOT NULL,
                    tokens REAL NOT NULL,
                    refill_rate REAL NOT NULL,
                    last_update TEXT NOT NULL
                )
            """
            )
            conn.commit()
        finally:
            if close_conn:
                conn.close()

    def _datetime_to_str(self, dt: datetime) -> str:
        """Convert datetime to ISO format string."""
        return dt.isoformat()

    def _str_to_datetime(self, s: str) -> datetime:
        """Convert ISO format string to datetime."""
        return datetime.fromisoformat(s)

    def _get_connection(self) -> sqlite3.Connection:
        """Get database connection, reusing for in-memory DB."""
        if self._conn is not None:
            return self._conn
        return self._connect()

    def get_rate_limit(self, endpoint: str) -> Optional[RateLimit]:
        """Get rate limit for an endpoint."""
        with self._lock:
            conn = self._get_connection()
            try:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute(
                    "SELECT * FROM rate_limits WHERE endpoint = ?", (endpoint,)
                )
                row = cursor.fetchone()
                if row is None:
                    return None

                return RateLimit(
                    endpoint=row["endpoint"],
                    limit=row["limit_value"],
                    remaining=row["remaining"],
                    reset_time=self._str_to_datetime(row["reset_time"]),
                    window=timedelta(seconds=row["window_seconds"]),
                    last_updated=self._str_to_datetime(row["last_updated"]),
                    confidence=row["confidence"],
                )
            finally:
                if self._conn is None:
                    conn.close()

    def set_rate_limit(self, endpoint: str, rate_limit: RateLimit) -> None:
        """Store rate limit for an endpoint."""
        with self._lock:
            conn = self._get_connection()
            try:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO rate_limits
                    (endpoint, limit_value, remaining, reset_time, window_seconds,
                     last_updated, confidence)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                    (
                        endpoint,
                        rate_limit.limit,
                        rate_limit.remaining,
                        self._datetime_to_str(rate_limit.reset_time),
                        rate_limit.window.total_seconds(),
                        self._datetime_to_str(rate_limit.last_updated),
                        rate_limit.confidence,
                    ),
                )
                conn.commit()
            finally:
                if self._conn is None:
                    conn.close()

    def get_token_bucket(self, key: str) -> Optional[TokenBucket]:
        """Get token bucket for a key."""
        with self._lock:
            conn = self._get_connection()
            try:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute(
                    "SELECT * FROM token_buckets WHERE key = ?", (key,)
                )
                row = cursor.fetchone()
                if row is None:
                    return None

                return TokenBucket(
                    capacity=row["capacity"],
                    tokens=row["tokens"],
                    refill_rate=row["refill_rate"],
                    last_update=self._str_to_datetime(row["last_update"]),
                )
            finally:
                if self._conn is None:
                    conn.close()

    def set_token_bucket(self, key: str, bucket: TokenBucket) -> None:
        """Store token bucket for a key."""
        with self._lock:
            conn = self._get_connection()
            try:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO token_buckets
                    (key, capacity, tokens, refill_rate, last_update)
                    VALUES (?, ?, ?, ?, ?)
                """,
                    (
                        key,
                        bucket.capacity,
                        bucket.tokens,
                        bucket.refill_rate,
                        self._datetime_to_str(bucket.last_update),
                    ),
                )
                conn.commit()
            finally:
                if self._conn is None:
                    conn.close()

    def clear(self, endpoint: Optional[str] = None) -> None:
        """Clear stored data for endpoint or all data."""
        with self._lock:
            conn = self._get_connection()
            try:
                if endpoint:
                    # Exact scope, plus everything nested under it.
                    conn.execute(
                        "DELETE FROM rate_limits "
                        "WHERE endpoint = ? OR endpoint LIKE ? OR endpoint LIKE ?",
                        (endpoint, f"{endpoint}/%", f"{endpoint}:%"),
                    )
                    conn.execute(
                        "DELETE FROM token_buckets WHERE key = ? OR key LIKE ? OR key LIKE ?",
                        (endpoint, f"{endpoint}/%", f"{endpoint}:%"),
                    )
                else:
                    conn.execute("DELETE FROM rate_limits")
                    conn.execute("DELETE FROM token_buckets")
                conn.commit()
            finally:
                if self._conn is None:
                    conn.close()

    def list_endpoints(self) -> List[str]:
        """List every endpoint key that currently has a stored rate limit."""
        with self._lock:
            conn = self._get_connection()
            try:
                cursor = conn.execute("SELECT endpoint FROM rate_limits")
                return [row[0] for row in cursor.fetchall()]
            finally:
                if self._conn is None:
                    conn.close()

    def get_rate_limit_for(self, candidates: List[str]) -> Optional[RateLimit]:
        """Resolve the most specific stored scope in one query."""
        if not candidates:
            return None

        with self._lock:
            conn = self._get_connection()
            try:
                conn.row_factory = sqlite3.Row
                placeholders = ",".join("?" for _ in candidates)
                cursor = conn.execute(
                    f"SELECT * FROM rate_limits WHERE endpoint IN ({placeholders})",
                    tuple(candidates),
                )
                rows = {row["endpoint"]: row for row in cursor.fetchall()}
            finally:
                if self._conn is None:
                    conn.close()

        # Candidate order is the precedence order, so honour it here rather
        # than letting SQL decide.
        for key in candidates:
            row = rows.get(key)
            if row is not None:
                return RateLimit(
                    endpoint=row["endpoint"],
                    limit=row["limit_value"],
                    remaining=row["remaining"],
                    reset_time=self._str_to_datetime(row["reset_time"]),
                    window=timedelta(seconds=row["window_seconds"]),
                    last_updated=self._str_to_datetime(row["last_updated"]),
                    confidence=row["confidence"],
                )
        return None

    def acquire(
        self,
        key: str,
        capacity: float,
        refill_rate: float,
        tokens: float = 1.0,
    ) -> Tuple[bool, float]:
        """
        Refill and consume inside a single write transaction.

        ``BEGIN IMMEDIATE`` takes SQLite's write lock up front, so the row read
        here cannot be read by another process until this transaction commits.
        That is what makes the bucket safe across processes, not merely across
        threads.
        """
        with self._lock:
            conn = self._get_connection()
            try:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    cursor = conn.execute(
                        "SELECT tokens, last_update FROM token_buckets WHERE key = ?",
                        (key,),
                    )
                    row = cursor.fetchone()
                    now = utcnow()

                    if row is None:
                        bucket = TokenBucket(
                            capacity=capacity,
                            tokens=capacity,
                            refill_rate=refill_rate,
                            last_update=now,
                        )
                    else:
                        bucket = TokenBucket(
                            capacity=capacity,
                            tokens=min(float(row[0]), capacity),
                            refill_rate=refill_rate,
                            last_update=self._str_to_datetime(row[1]),
                        )

                    allowed = bucket.consume(tokens, now=now)
                    wait = 0.0 if allowed else bucket.wait_time(tokens, now=now)

                    conn.execute(
                        """
                        INSERT OR REPLACE INTO token_buckets
                        (key, capacity, tokens, refill_rate, last_update)
                        VALUES (?, ?, ?, ?, ?)
                    """,
                        (
                            key,
                            bucket.capacity,
                            bucket.tokens,
                            bucket.refill_rate,
                            self._datetime_to_str(bucket.last_update),
                        ),
                    )
                    conn.execute("COMMIT")
                except Exception:
                    conn.execute("ROLLBACK")
                    raise
                return allowed, wait
            finally:
                if self._conn is None:
                    conn.close()


class RedisStorage(StorageBackend):
    """Redis-based distributed storage backend."""

    #: Refill-and-consume as a single Lua script.
    #:
    #: Redis runs a script to completion without interleaving other commands,
    #: which is what makes this a genuine distributed limiter: every worker's
    #: refill/check/consume happens as one indivisible step. Reading the bucket
    #: with HGETALL and writing it back from Python would let two workers see
    #: the same token count and each spend it.
    #:
    #: The clock comes from the Redis server (``TIME``) rather than from each
    #: caller, so workers with skewed clocks still refill consistently.
    _ACQUIRE_LUA = """
    local key = KEYS[1]
    local capacity = tonumber(ARGV[1])
    local refill_rate = tonumber(ARGV[2])
    local requested = tonumber(ARGV[3])
    local ttl = tonumber(ARGV[4])

    local time = redis.call('TIME')
    local now = tonumber(time[1]) + (tonumber(time[2]) / 1000000)

    local state = redis.call('HMGET', key, 'tokens', 'last_update')
    local tokens = tonumber(state[1])
    local last_update = tonumber(state[2])

    if tokens == nil or last_update == nil then
        tokens = capacity
        last_update = now
    end

    -- The limit may have been revised downward since the bucket was written.
    if tokens > capacity then
        tokens = capacity
    end

    local elapsed = now - last_update
    if elapsed > 0 then
        tokens = math.min(capacity, tokens + (elapsed * refill_rate))
        last_update = now
    end

    local allowed = 0
    local wait = 0
    if tokens >= requested then
        tokens = tokens - requested
        allowed = 1
    elseif refill_rate > 0 then
        wait = (requested - tokens) / refill_rate
    else
        wait = -1
    end

    redis.call('HSET', key,
        'capacity', capacity,
        'tokens', tokens,
        'refill_rate', refill_rate,
        'last_update', last_update)
    redis.call('EXPIRE', key, ttl)

    return {allowed, tostring(wait)}
    """

    def __init__(
        self,
        redis_url: str = "redis://localhost:6379/0",
        key_prefix: str = "ratelimit:",
        fail_closed: bool = False,
    ):
        """
        Initialize Redis storage.

        Args:
            redis_url: Redis connection URL
            key_prefix: Prefix for all keys stored in Redis
            fail_closed: What to do when Redis cannot be reached during a call.
                The default (False) fails *open* — the request is allowed
                through unpaced, so a Redis outage cannot take your job down
                with it. Set True when the limit guards something costly, such
                as a paid API quota, and sending unpaced traffic is worse than
                raising: ``acquire`` then raises
                :class:`~smartratelimit._base.StorageUnavailable` instead of
                waving requests through.
        """
        try:
            import redis
        except ImportError:
            raise ImportError(
                "Redis support requires the 'redis' package. "
                "Install it with: pip install redis"
            )

        self.redis_client = redis.from_url(redis_url, decode_responses=False)
        self.key_prefix = key_prefix
        self.fail_closed = fail_closed
        self._lock = threading.RLock()
        self._acquire_script = self.redis_client.register_script(self._ACQUIRE_LUA)

        # redis-py connects lazily, so without this a dead Redis looks healthy
        # until the first request -- and then quietly stops limiting. Check now
        # and say so. The client is kept either way: a Redis that is briefly
        # down at boot comes back, and swapping permanently to per-process
        # limits would mean never noticing that it did.
        try:
            self.redis_client.ping()
        except Exception as e:
            if fail_closed:
                raise ConnectionError(f"cannot reach Redis at {redis_url}: {e}") from e
            logger.warning(
                "Cannot reach Redis at %s (%s). Requests will not be paced until "
                "it returns. Pass fail_closed=True to fail fast instead.",
                redis_url,
                e,
            )

    def _make_key(self, key: str) -> bytes:
        """Create a Redis key with prefix."""
        return f"{self.key_prefix}{key}".encode("utf-8")

    def _datetime_to_str(self, dt: datetime) -> str:
        """Convert datetime to ISO format string."""
        return dt.isoformat()

    def _str_to_datetime(self, s: bytes) -> datetime:
        """Convert bytes to datetime."""
        return datetime.fromisoformat(s.decode("utf-8"))

    def _parse_bucket_timestamp(self, value: bytes) -> datetime:
        """
        Read a bucket timestamp written by either the Lua script or Python.

        The Lua script cannot format ISO strings, so it stores ``last_update``
        as a Unix timestamp. Buckets written by older versions of this library
        hold an ISO string, so both are accepted.
        """
        text = value.decode("utf-8")
        try:
            return _from_epoch(float(text))
        except ValueError:
            return datetime.fromisoformat(text)

    def get_rate_limit(self, endpoint: str) -> Optional[RateLimit]:
        """Get rate limit for an endpoint."""
        with self._lock:
            try:
                key = self._make_key(f"rate_limit:{endpoint}")
                data = self.redis_client.hgetall(key)
                if not data:
                    return None

                return RateLimit(
                    endpoint=endpoint,
                    limit=int(data[b"limit"]),
                    remaining=int(data[b"remaining"]),
                    reset_time=self._str_to_datetime(data[b"reset_time"]),
                    window=timedelta(seconds=float(data[b"window_seconds"])),
                    last_updated=self._str_to_datetime(data[b"last_updated"]),
                    confidence=data.get(b"confidence", b"confirmed").decode("utf-8"),
                )
            except Exception as e:
                logger.debug("Redis read failed (%s); treating as no stored value", e)
                return None

    def set_rate_limit(self, endpoint: str, rate_limit: RateLimit) -> None:
        """Store rate limit for an endpoint."""
        with self._lock:
            try:
                key = self._make_key(f"rate_limit:{endpoint}")
                data = {
                    b"limit": str(rate_limit.limit).encode("utf-8"),
                    b"remaining": str(rate_limit.remaining).encode("utf-8"),
                    b"reset_time": self._datetime_to_str(rate_limit.reset_time).encode("utf-8"),
                    b"window_seconds": str(rate_limit.window.total_seconds()).encode("utf-8"),
                    b"last_updated": self._datetime_to_str(rate_limit.last_updated).encode("utf-8"),
                    b"confidence": rate_limit.confidence.encode("utf-8"),
                }
                self.redis_client.hset(key, mapping=data)
                # Set expiration to window + 1 hour for cleanup
                ttl = int((rate_limit.window + timedelta(hours=1)).total_seconds())
                self.redis_client.expire(key, ttl)
            except Exception as e:
                logger.warning("Redis write failed (%s); state not persisted", e)

    def get_token_bucket(self, key: str) -> Optional[TokenBucket]:
        """Get token bucket for a key."""
        with self._lock:
            try:
                redis_key = self._make_key(f"token_bucket:{key}")
                data = self.redis_client.hgetall(redis_key)
                if not data:
                    return None

                return TokenBucket(
                    capacity=float(data[b"capacity"]),
                    tokens=float(data[b"tokens"]),
                    refill_rate=float(data[b"refill_rate"]),
                    last_update=self._parse_bucket_timestamp(data[b"last_update"]),
                )
            except Exception as e:
                logger.debug("Redis read failed (%s); treating as no stored value", e)
                return None

    def set_token_bucket(self, key: str, bucket: TokenBucket) -> None:
        """Store token bucket for a key."""
        with self._lock:
            try:
                redis_key = self._make_key(f"token_bucket:{key}")
                data = {
                    b"capacity": str(bucket.capacity).encode("utf-8"),
                    b"tokens": str(bucket.tokens).encode("utf-8"),
                    b"refill_rate": str(bucket.refill_rate).encode("utf-8"),
                    b"last_update": str(_to_epoch(bucket.last_update)).encode("utf-8"),
                }
                self.redis_client.hset(redis_key, mapping=data)
                # Set expiration to 24 hours for cleanup
                self.redis_client.expire(redis_key, 86400)
            except Exception as e:
                logger.warning("Redis write failed (%s); state not persisted", e)

    def clear(self, endpoint: Optional[str] = None) -> None:
        """Clear stored data for endpoint or all data."""
        with self._lock:
            try:
                if endpoint:
                    # Exact scope, plus everything nested under it. Scanning and
                    # filtering in Python keeps the boundary rule identical to
                    # the other backends, and avoids glob metacharacters in a
                    # URL being interpreted as a pattern.
                    for namespace in ("rate_limit:", "token_bucket:"):
                        prefix = f"{self.key_prefix}{namespace}"
                        pattern = f"{prefix}*".encode("utf-8")
                        for key in self.redis_client.scan_iter(match=pattern):
                            stored = key.decode("utf-8")[len(prefix):]
                            if _covers(endpoint, stored):
                                self.redis_client.delete(key)
                else:
                    # Delete all keys with prefix
                    pattern = self._make_key("*")
                    for key in self.redis_client.scan_iter(match=pattern):
                        self.redis_client.delete(key)
            except Exception as e:
                logger.warning("Redis write failed (%s); state not persisted", e)

    def list_endpoints(self) -> List[str]:
        """List every endpoint key that currently has a stored rate limit."""
        prefix = f"{self.key_prefix}rate_limit:"
        try:
            pattern = f"{prefix}*".encode("utf-8")
            return [
                key.decode("utf-8")[len(prefix):]
                for key in self.redis_client.scan_iter(match=pattern)
            ]
        except Exception as e:
            logger.warning("Redis scan failed (%s); reporting no endpoints", e)
            return []

    def get_rate_limit_for(self, candidates: List[str]) -> Optional[RateLimit]:
        """Resolve the most specific stored scope in one pipelined round trip."""
        if not candidates:
            return None

        try:
            pipe = self.redis_client.pipeline()
            for key in candidates:
                pipe.hgetall(self._make_key(f"rate_limit:{key}"))
            results = pipe.execute()
        except Exception as e:
            logger.debug("Redis pipeline failed (%s); treating as no stored value", e)
            return None

        # Candidate order is the precedence order.
        for key, data in zip(candidates, results):
            if not data:
                continue
            try:
                return RateLimit(
                    endpoint=key,
                    limit=int(data[b"limit"]),
                    remaining=int(data[b"remaining"]),
                    reset_time=self._str_to_datetime(data[b"reset_time"]),
                    window=timedelta(seconds=float(data[b"window_seconds"])),
                    last_updated=self._str_to_datetime(data[b"last_updated"]),
                    confidence=data.get(b"confidence", b"confirmed").decode("utf-8"),
                )
            except (KeyError, ValueError) as e:
                logger.debug("Skipping malformed rate limit for %s: %s", key, e)
        return None

    def acquire(
        self,
        key: str,
        capacity: float,
        refill_rate: float,
        tokens: float = 1.0,
    ) -> Tuple[bool, float]:
        """Refill and consume atomically via a server-side Lua script."""
        redis_key = self._make_key(f"token_bucket:{key}")
        # Keep the bucket alive well past a full refill so a quiet endpoint
        # does not silently reset to full capacity mid-window.
        ttl = max(86400, int((capacity / refill_rate) * 2) if refill_rate > 0 else 86400)

        try:
            allowed, wait = self._acquire_script(
                keys=[redis_key],
                args=[capacity, refill_rate, tokens, ttl],
            )
        except Exception as e:
            if self.fail_closed:
                from smartratelimit._base import StorageUnavailable

                raise StorageUnavailable(
                    f"Redis is unavailable and fail_closed=True, so {key!r} "
                    f"cannot be paced: {e}"
                ) from e

            # Fail open: a limiter outage should not take the caller's traffic
            # down with it. Loud, because the shared limit is not being
            # enforced while this lasts.
            logger.warning(
                "Redis unavailable (%s) -- allowing request for %s unpaced. "
                "Pass fail_closed=True to raise instead.",
                e,
                key,
            )
            return True, 0.0

        wait_seconds = float(wait)
        if wait_seconds < 0:
            wait_seconds = float("inf")
        return bool(allowed), 0.0 if allowed else wait_seconds

