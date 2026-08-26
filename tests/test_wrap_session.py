"""Tests for wrap_session preserving the caller's session as transport."""

import requests

from smartratelimit import RateLimiter


class RecordingAdapter(requests.adapters.HTTPAdapter):
    """Captures the PreparedRequest instead of hitting the network."""

    def __init__(self):
        super().__init__()
        self.sent = []

    def send(self, request, **kwargs):
        self.sent.append(request)
        response = requests.Response()
        response.status_code = 200
        response.url = request.url
        response.request = request
        return response


def make_session():
    session = requests.Session()
    adapter = RecordingAdapter()
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session, adapter


class TestWrapSession:
    """A wrapped session must still be the session that makes the call."""

    def test_uses_the_wrapped_sessions_adapter(self):
        """The limiter's own session would never reach this adapter."""
        session, adapter = make_session()
        RateLimiter().wrap_session(session)

        response = session.request("GET", "https://api.example.com/test")

        assert response.status_code == 200
        assert len(adapter.sent) == 1

    def test_preserves_session_headers(self):
        session, adapter = make_session()
        session.headers.update({"X-Api-Key": "secret", "User-Agent": "my-app/1.0"})
        RateLimiter().wrap_session(session)

        session.request("GET", "https://api.example.com/test")

        sent = adapter.sent[0]
        assert sent.headers["X-Api-Key"] == "secret"
        assert sent.headers["User-Agent"] == "my-app/1.0"

    def test_preserves_session_auth(self):
        session, adapter = make_session()
        session.auth = ("user", "pass")
        RateLimiter().wrap_session(session)

        session.request("GET", "https://api.example.com/test")

        assert adapter.sent[0].headers["Authorization"].startswith("Basic ")

    def test_preserves_session_cookies(self):
        session, adapter = make_session()
        session.cookies.set("session_id", "abc123", domain="api.example.com")
        RateLimiter().wrap_session(session)

        session.request("GET", "https://api.example.com/test")

        assert "session_id=abc123" in adapter.sent[0].headers["Cookie"]

    def test_per_request_kwargs_still_work(self):
        session, adapter = make_session()
        RateLimiter().wrap_session(session)

        session.request(
            "GET", "https://api.example.com/test", headers={"X-Request-Id": "42"}
        )

        assert adapter.sent[0].headers["X-Request-Id"] == "42"

    def test_still_rate_limits(self):
        """Preserving the transport must not mean skipping the limiter."""
        session, adapter = make_session()
        limiter = RateLimiter(raise_on_limit=True)
        limiter.set_limit("api.example.com", limit=1, window="1h")
        limiter.wrap_session(session)

        session.request("GET", "https://api.example.com/test")

        try:
            session.request("GET", "https://api.example.com/test")
        except Exception as exc:
            assert "Rate limit exceeded" in str(exc)
        else:
            raise AssertionError("second request should have been rate limited")

    def test_wrapping_twice_does_not_recurse(self):
        session, adapter = make_session()
        limiter = RateLimiter()
        limiter.wrap_session(session)
        limiter.wrap_session(session)

        assert session.request("GET", "https://api.example.com/x").status_code == 200
        assert len(adapter.sent) == 1

    def test_convenience_methods_go_through_the_limiter(self):
        """session.get() delegates to session.request(), so it is covered too."""
        session, adapter = make_session()
        RateLimiter().wrap_session(session)

        assert session.get("https://api.example.com/test").status_code == 200
        assert len(adapter.sent) == 1
