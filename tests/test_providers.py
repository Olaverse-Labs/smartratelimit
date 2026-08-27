"""Tests for the built-in provider registry."""

from unittest.mock import Mock, patch

import pytest
from requests.structures import CaseInsensitiveDict

from smartratelimit import Provider, RateLimiter, SeedLimit, register_provider
from smartratelimit.providers import PROVIDERS, get_provider, seed_limits


def make_response(status_code=200, headers=None, url="https://api.github.com/users"):
    response = Mock()
    response.url = url
    response.status_code = status_code
    response.headers = CaseInsensitiveDict(headers or {})
    return response


class TestRegistryScope:
    """The registry earns its place by staying small and defensible."""

    def test_registry_is_deliberately_small(self):
        """A large table would be a table of guesses about other people's accounts."""
        assert len(PROVIDERS) <= 5, (
            "New entries must clear both bars in providers.py: undetectable in "
            "time to be useful, and a property of the API rather than of the "
            "caller's account."
        )

    def test_every_entry_explains_itself(self):
        for host, provider in PROVIDERS.items():
            assert provider.reason.strip(), f"{host} has no stated reason"

    @pytest.mark.parametrize("host", ["api.openai.com", "api.stripe.com", "api.anthropic.com"])
    def test_per_account_providers_are_excluded(self, host):
        """These meter per account tier, so a baked-in number would be a guess."""
        assert get_provider(host) is None

    def test_unknown_host_seeds_nothing(self):
        assert seed_limits("api.example.com") == {}


class TestGitHubProfile:
    """The one case detection genuinely cannot reach in time."""

    def test_anonymous_and_authenticated_differ(self):
        anon = seed_limits("api.github.com")
        auth = seed_limits("api.github.com", authenticated=True)

        assert anon[""][0].limit == 60
        assert auth[""][0].limit == 5000

    def test_search_is_scoped_more_tightly_than_the_host(self):
        anon = seed_limits("api.github.com")

        assert anon["/search"][0].limit == 10
        assert anon["/search"][0].window == "1m"

    def test_host_lookup_is_case_insensitive(self):
        assert get_provider("API.GitHub.COM") is not None


class TestSeeding:
    """Seeds apply before the first response and yield to real headers."""

    def test_limits_are_seeded_before_any_request(self):
        limiter = RateLimiter()
        limiter._apply_provider_profile("https://api.github.com/users/octocat")

        status = limiter.get_status("https://api.github.com")
        assert status.limit == 60
        assert status.confidence == "registry"

    def test_authenticated_seeds_the_higher_limit(self):
        limiter = RateLimiter(authenticated=True)
        limiter._apply_provider_profile("https://api.github.com/users/octocat")

        assert limiter.get_status("https://api.github.com").limit == 5000

    def test_search_scope_wins_for_search_paths(self):
        limiter = RateLimiter()
        limiter._apply_provider_profile("https://api.github.com/search/code")

        assert limiter._resolve_limit("https://api.github.com/search/code").limit == 10
        assert limiter._resolve_limit("https://api.github.com/users").limit == 60

    def test_seeding_can_be_turned_off(self):
        limiter = RateLimiter(use_provider_profiles=False)
        limiter._apply_provider_profile("https://api.github.com/users")

        assert limiter.get_status("https://api.github.com") is None

    def test_seeds_do_not_overwrite_a_configured_limit(self):
        limiter = RateLimiter()
        limiter.set_limit("api.github.com", limit=7, window="1m")
        limiter._apply_provider_profile("https://api.github.com/users")

        status = limiter.get_status("api.github.com")
        assert status.limit == 7
        assert status.confidence == "configured"

    @patch("smartratelimit.core.requests.Session.request")
    def test_real_headers_replace_the_seed(self, mock_request):
        """A seed is a placeholder, not an answer."""
        mock_request.return_value = make_response(
            headers={
                "X-RateLimit-Limit": "5000",
                "X-RateLimit-Remaining": "4999",
                "X-RateLimit-Reset": "3600",
            }
        )

        limiter = RateLimiter()
        limiter.request("GET", "https://api.github.com/users/octocat")

        status = limiter.get_status("api.github.com")
        assert status.limit == 5000
        assert status.confidence == "confirmed"

    @patch("smartratelimit.core.requests.Session.request")
    def test_seed_paces_the_very_first_request(self, mock_request):
        """60/hour is low enough that learning it from response one is too late."""
        mock_request.return_value = make_response(headers={})

        limiter = RateLimiter(raise_on_limit=True)
        limiter.request("GET", "https://api.github.com/users/octocat")

        status = limiter.get_status("api.github.com")
        assert status.limit == 60
        assert status.remaining == 59

    def test_lookup_happens_once_per_host(self):
        """A miss is the common case; re-deciding it per request is waste."""
        limiter = RateLimiter()

        with patch("smartratelimit._base.seed_limits", return_value={}) as mock_seed:
            for _ in range(5):
                limiter._apply_provider_profile("https://api.example.com/a")

        assert mock_seed.call_count == 1


class TestCustomProviders:
    """Your own undocumented internal API is exactly the intended case."""

    def test_register_and_seed(self):
        register_provider(
            "internal.test",
            Provider(
                name="Internal service",
                reason="Sends no rate-limit headers; limit is in the runbook.",
                scopes={"": [SeedLimit(25, "1m")]},
            ),
        )
        try:
            limiter = RateLimiter()
            limiter._apply_provider_profile("https://internal.test/v1/things")

            assert limiter.get_status("internal.test").limit == 25
        finally:
            PROVIDERS.pop("internal.test", None)

    def test_custom_provider_can_seed_extra_dimensions(self):
        register_provider(
            "llm.test",
            Provider(
                name="Internal LLM gateway",
                reason="No headers; limits published internally.",
                scopes={
                    "": [SeedLimit(60, "1m"), SeedLimit(50000, "1m", dimension="tokens")]
                },
            ),
        )
        try:
            limiter = RateLimiter()
            limiter._apply_provider_profile("https://llm.test/v1/chat")

            status = limiter.get_status("llm.test")
            assert status.dimensions["requests"].limit == 60
            assert status.dimensions["tokens"].limit == 50000
        finally:
            PROVIDERS.pop("llm.test", None)
