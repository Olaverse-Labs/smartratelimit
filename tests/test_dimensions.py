"""Tests for multi-dimensional limits and per-request cost."""

import os
import tempfile
from unittest.mock import Mock, patch

import pytest
from requests.structures import CaseInsensitiveDict

from smartratelimit import RateLimiter, RateLimitExceeded
from smartratelimit.storage import MemoryStorage, RedisStorage, SQLiteStorage

REDIS_URL = "redis://localhost:6379/0"
REDIS_PREFIX = "smartratelimit-dim-test:"


def redis_available():
    try:
        import redis

        redis.from_url(REDIS_URL).ping()
        return True
    except Exception:
        return False


requires_redis = pytest.mark.skipif(not redis_available(), reason="Redis not available")

# A day-long window: refill over the length of a test is negligible.
SLOW = 86400.0


def make_response(status_code=200, headers=None, url="https://api.example.com/test"):
    response = Mock()
    response.url = url
    response.status_code = status_code
    response.headers = CaseInsensitiveDict(headers or {})
    return response


@pytest.fixture(params=["memory", "sqlite", pytest.param("redis", marks=requires_redis)])
def storage(request):
    if request.param == "memory":
        return MemoryStorage()
    if request.param == "sqlite":
        return SQLiteStorage(os.path.join(tempfile.mkdtemp(), "dims.db"))

    backend = RedisStorage(redis_url=REDIS_URL, key_prefix=REDIS_PREFIX)
    backend.clear()
    request.addfinalizer(backend.clear)
    return backend


class TestAcquireMany:
    """All-or-nothing across buckets is the whole point."""

    def test_charges_every_bucket_on_success(self, storage):
        allowed, wait = storage.acquire_many(
            [("rpm", 100.0, 100 / SLOW, 1.0), ("tpm", 10000.0, 10000 / SLOW, 2000.0)]
        )

        assert allowed is True
        assert wait == 0.0
        assert storage.get_token_bucket("rpm").tokens == pytest.approx(99.0, abs=0.1)
        assert storage.get_token_bucket("tpm").tokens == pytest.approx(8000.0, abs=1.0)

    def test_charges_nothing_when_one_bucket_refuses(self, storage):
        """The leak this prevents: request spent, tokens refused, nothing sent."""
        specs = [("rpm", 100.0, 100 / SLOW, 1.0), ("tpm", 10000.0, 10000 / SLOW, 4000.0)]

        for _ in range(2):
            assert storage.acquire_many(specs)[0] is True

        # 12000 tokens requested against 10000: refused.
        allowed, wait = storage.acquire_many(specs)
        assert allowed is False
        assert wait > 0

        # Two grants, so exactly two requests charged -- not three.
        assert storage.get_token_bucket("rpm").tokens == pytest.approx(98.0, abs=0.1)
        assert storage.get_token_bucket("tpm").tokens == pytest.approx(2000.0, abs=1.0)

    def test_wait_is_the_longest_of_the_refusals(self, storage):
        # 2 per minute and 4 per minute; both exhausted, requests refills slower.
        specs = [("slow", 2.0, 2 / 60.0, 1.0), ("fast", 4.0, 4 / 60.0, 1.0)]
        for _ in range(2):
            storage.acquire_many(specs)

        allowed, wait = storage.acquire_many(specs)
        assert allowed is False
        # One token at 2/min is 30s away; at 4/min it is 15s. Wait for both.
        assert wait == pytest.approx(30.0, abs=2.0)

    def test_single_spec_matches_acquire(self, storage):
        allowed, wait = storage.acquire_many([("k", 5.0, 5 / SLOW, 1.0)])
        assert (allowed, wait) == (True, 0.0)
        assert storage.get_token_bucket("k").tokens == pytest.approx(4.0, abs=0.1)

    def test_zero_cost_dimension_is_not_gated(self, storage):
        """A dimension a request does not spend must not block it."""
        specs = [("rpm", 100.0, 100 / SLOW, 1.0)]
        for _ in range(50):
            assert storage.acquire_many(specs)[0] is True


class TestCostNormalization:
    """What a caller passes as `cost`."""

    def test_none_is_one_request(self):
        assert RateLimiter._normalize_cost(None) == {"requests": 1.0}

    def test_number_is_that_many_requests(self):
        assert RateLimiter._normalize_cost(3) == {"requests": 3.0}

    def test_mapping_defaults_requests_to_one(self):
        """A 1500-token call is still one request unless you say otherwise."""
        assert RateLimiter._normalize_cost({"tokens": 1500}) == {
            "requests": 1.0,
            "tokens": 1500.0,
        }

    def test_mapping_can_override_requests(self):
        assert RateLimiter._normalize_cost({"requests": 0, "tokens": 10}) == {
            "requests": 0.0,
            "tokens": 10.0,
        }

    def test_rejects_nonsense(self):
        with pytest.raises(TypeError):
            RateLimiter._normalize_cost(["tokens"])


class TestMultiDimensionalLimits:
    """Two budgets on one scope, both binding."""

    @pytest.fixture
    def limiter(self, storage):
        limiter = RateLimiter(storage=storage, raise_on_limit=True)
        limiter.set_limit("api.llm.test", limit=100, window="1d")
        limiter.set_limit("api.llm.test", limit=10000, window="1d", dimension="tokens")
        return limiter

    def test_both_dimensions_are_stored(self, limiter):
        status = limiter.get_status("api.llm.test")

        assert status.dimensions["requests"].limit == 100
        assert status.dimensions["tokens"].limit == 10000

    def test_token_budget_binds_before_request_budget(self, limiter):
        """The OpenAI case: plenty of requests left, no tokens left."""
        rate_limit = limiter._resolve_limit("https://api.llm.test/v1/chat")

        for _ in range(5):
            limiter._acquire_limit(rate_limit, {"tokens": 2000})

        with pytest.raises(RateLimitExceeded):
            limiter._acquire_limit(rate_limit, {"tokens": 2000})

        status = limiter.get_status("api.llm.test")
        assert status.dimensions["tokens"].remaining == 0
        # Five spent, and the refused sixth charged nothing.
        assert status.dimensions["requests"].remaining == 95

    def test_request_budget_still_binds(self, limiter):
        limiter.clear("api.llm.test")
        limiter.set_limit("api.llm.test", limit=2, window="1d")
        limiter.set_limit("api.llm.test", limit=10000, window="1d", dimension="tokens")
        rate_limit = limiter._resolve_limit("https://api.llm.test/v1/chat")

        for _ in range(2):
            limiter._acquire_limit(rate_limit, {"tokens": 1})

        with pytest.raises(RateLimitExceeded):
            limiter._acquire_limit(rate_limit, {"tokens": 1})

    def test_untouched_dimension_does_not_gate(self, limiter):
        """A call declaring no token cost is not held up by the token budget."""
        rate_limit = limiter._resolve_limit("https://api.llm.test/v1/models")

        for _ in range(5):
            limiter._acquire_limit(rate_limit, {"tokens": 2000})

        # Token budget is spent, but this call spends none of it.
        limiter._acquire_limit(rate_limit, None)

    def test_unknown_dimension_warns(self, limiter, caplog):
        import logging

        rate_limit = limiter._resolve_limit("https://api.llm.test/v1/chat")

        with caplog.at_level(logging.WARNING):
            limiter._acquire_limit(rate_limit, {"widgets": 5})

        assert any("widgets" in r.getMessage() for r in caplog.records)

    def test_dimensions_survive_a_round_trip(self, limiter):
        """Extra dimensions must persist, not just live in memory."""
        reloaded = RateLimiter(storage=limiter._storage)
        status = reloaded.get_status("api.llm.test")

        assert status.dimensions["tokens"].limit == 10000
        assert status.dimensions["tokens"].confidence == "configured"


class TestCostThroughRequest:
    """cost= on the public request path."""

    @patch("smartratelimit.core.requests.Session.request")
    def test_request_accepts_cost(self, mock_request):
        mock_request.return_value = make_response(url="https://api.llm.test/v1/chat")

        limiter = RateLimiter(raise_on_limit=True)
        limiter.set_limit("api.llm.test", limit=100, window="1d")
        limiter.set_limit("api.llm.test", limit=1000, window="1d", dimension="tokens")

        limiter.request("POST", "https://api.llm.test/v1/chat", cost={"tokens": 600})
        assert limiter.get_status("api.llm.test").dimensions["tokens"].remaining == 400

        with pytest.raises(RateLimitExceeded):
            limiter.request("POST", "https://api.llm.test/v1/chat", cost={"tokens": 600})

    @patch("smartratelimit.core.requests.Session.request")
    def test_cost_is_not_forwarded_to_the_transport(self, mock_request):
        mock_request.return_value = make_response()

        limiter = RateLimiter()
        limiter.request("GET", "https://api.example.com/test", cost={"tokens": 1})

        assert "cost" not in mock_request.call_args.kwargs


class TestDimensionDetection:
    """Token headers must be read, not just request headers."""

    def test_openai_token_headers_are_detected(self):
        from smartratelimit.detector import RateLimitDetector

        detections = RateLimitDetector().detect_all(
            "https://api.openai.com/v1/chat/completions",
            200,
            CaseInsensitiveDict(
                {
                    "x-ratelimit-limit-requests": "3500",
                    "x-ratelimit-remaining-requests": "3499",
                    "x-ratelimit-reset-requests": "60",
                    "x-ratelimit-limit-tokens": "90000",
                    "x-ratelimit-remaining-tokens": "88500",
                    "x-ratelimit-reset-tokens": "60",
                }
            ),
        )

        by_name = {d["dimension"]: d for d in detections}
        assert by_name["requests"]["limit"] == 3500
        assert by_name["tokens"]["limit"] == 90000
        assert by_name["tokens"]["remaining"] == 88500

    def test_generic_token_headers_are_detected(self):
        from smartratelimit.detector import RateLimitDetector

        detections = RateLimitDetector().detect_all(
            "https://api.example.com/v1/x",
            200,
            CaseInsensitiveDict(
                {
                    "X-RateLimit-Limit": "100",
                    "X-RateLimit-Reset": "60",
                    "X-RateLimit-Limit-Tokens": "5000",
                    "X-RateLimit-Reset-Tokens": "60",
                }
            ),
        )

        assert {d["dimension"] for d in detections} == {"requests", "tokens"}

    @patch("smartratelimit.core.requests.Session.request")
    def test_detected_token_limit_reaches_status(self, mock_request):
        mock_request.return_value = make_response(
            url="https://api.openai.com/v1/chat/completions",
            headers={
                "x-ratelimit-limit-requests": "3500",
                "x-ratelimit-remaining-requests": "3499",
                "x-ratelimit-reset-requests": "60",
                "x-ratelimit-limit-tokens": "90000",
                "x-ratelimit-remaining-tokens": "88500",
                "x-ratelimit-reset-tokens": "60",
            },
        )

        limiter = RateLimiter()
        limiter.request("POST", "https://api.openai.com/v1/chat/completions")

        status = limiter.get_status("api.openai.com")
        assert status.dimensions["requests"].limit == 3500
        assert status.dimensions["tokens"].limit == 90000

    def test_single_dimension_response_is_unchanged(self):
        from smartratelimit.detector import RateLimitDetector

        detections = RateLimitDetector().detect_all(
            "https://api.example.com/x",
            200,
            CaseInsensitiveDict({"X-RateLimit-Limit": "100", "X-RateLimit-Reset": "60"}),
        )

        assert len(detections) == 1
        assert detections[0]["dimension"] == "requests"


# 40 requests and 10000 tokens; each call costs 1 request and 500 tokens, so the
# token budget binds first, at exactly 20 calls.
MP_RPM, MP_TPM, MP_TOKEN_COST = 40.0, 10000.0, 500.0
MP_PREFIX = "smartratelimit-dim-mp:"


def _redis_multi_worker(attempts, queue):
    """Module-level so it can be pickled for spawn-based start methods."""
    backend = RedisStorage(redis_url=REDIS_URL, key_prefix=MP_PREFIX)
    specs = [
        ("rpm", MP_RPM, MP_RPM / SLOW, 1.0),
        ("tpm", MP_TPM, MP_TPM / SLOW, MP_TOKEN_COST),
    ]
    queue.put(sum(backend.acquire_many(specs)[0] for _ in range(attempts)))


@requires_redis
class TestRedisMultiDimensionalContention:
    """Multiple dimensions, multiple processes, no partial charges."""

    def test_no_dimension_is_overdrawn_or_half_charged(self):
        import multiprocessing

        backend = RedisStorage(redis_url=REDIS_URL, key_prefix=MP_PREFIX)
        backend.clear()

        try:
            queue = multiprocessing.Queue()
            processes = [
                multiprocessing.Process(target=_redis_multi_worker, args=(15, queue))
                for _ in range(8)
            ]
            for p in processes:
                p.start()

            granted = sum(queue.get() for _ in processes)
            for p in processes:
                p.join()

            # 120 attempts; the token budget allows exactly 20.
            assert granted == 20

            requests_charged = MP_RPM - backend.get_token_bucket("rpm").tokens
            tokens_charged = MP_TPM - backend.get_token_bucket("tpm").tokens

            # The leak this guards: a refused call must not have charged the
            # request bucket on its way to being refused for tokens.
            assert requests_charged == pytest.approx(granted, abs=0.5)
            assert tokens_charged == pytest.approx(granted * MP_TOKEN_COST, abs=1.0)
        finally:
            backend.clear()
