"""Built-in provider profiles: seed limits for what headers cannot tell you.

**This registry is deliberately tiny, and it should stay that way.**

The tempting version of this file is a big table of every popular API's limits.
That table would be wrong more often than right. Most providers meter per
account tier — OpenAI's requests-per-minute depends on which tier you are on,
not on OpenAI — so a baked-in number is a guess about *your account* that this
library is in no position to make. A wrong baked-in limit is worse than none: it
either throttles you against a ceiling that does not exist, or lets you sail
past the real one, and in both cases nothing tells you.

Those providers also *send their limits in headers*, which detection already
reads. A registry entry would duplicate live, account-correct data with a stale
copy.

So an entry earns its place only when both of these hold:

1. The number cannot be detected in time to be useful — either the API sends no
   rate-limit headers at all, or you need to know the limit *before* the first
   response teaches it to you.
2. The number is a published property of the API itself, not of your account.

Everything here is a **seed**: it is stored with ``confidence="registry"`` and
is replaced the moment the API reports its own numbers. It gets you a sensible
first request, not a permanent answer.
"""

import logging
from typing import Callable, Dict, List, NamedTuple, Optional

logger = logging.getLogger(__name__)


class SeedLimit(NamedTuple):
    """One dimension of a provider's documented limit."""

    limit: int
    window: str
    dimension: str = "requests"


class Provider(NamedTuple):
    """A profile of documented limits for one API."""

    name: str
    #: Why detection cannot supply this, so future readers can tell whether the
    #: entry still earns its place.
    reason: str
    #: Scope suffix -> limits. ``""`` is the host itself; ``"/search"`` narrows
    #: to that path prefix.
    scopes: Dict[str, List[SeedLimit]]
    #: Same, applied instead when the caller says the client is authenticated.
    authenticated_scopes: Optional[Dict[str, List[SeedLimit]]] = None


#: Host -> profile. Additions need to clear both bars in the module docstring.
PROVIDERS: Dict[str, Provider] = {
    "api.github.com": Provider(
        name="GitHub REST API",
        reason=(
            "GitHub does send X-RateLimit headers, but the unauthenticated "
            "ceiling is 60 requests an hour — low enough that learning it from "
            "the first response wastes a meaningful slice of the budget. The "
            "numbers are properties of the API, published and long stable, not "
            "of your account."
        ),
        scopes={
            "": [SeedLimit(60, "1h")],
            "/search": [SeedLimit(10, "1m")],
        },
        authenticated_scopes={
            "": [SeedLimit(5000, "1h")],
            "/search": [SeedLimit(30, "1m")],
        },
    ),
}


def get_provider(host: str) -> Optional[Provider]:
    """
    Look up a provider profile by hostname.

    Args:
        host: Bare hostname, e.g. ``'api.github.com'``.

    Returns:
        The profile, or None if this host has no entry.
    """
    return PROVIDERS.get(host.lower())


def register_provider(host: str, provider: Provider) -> None:
    """
    Add or replace a provider profile at runtime.

    The built-in table stays small on purpose, but your own APIs — internal
    services that document a limit and send no headers — are exactly the case it
    is for.

    Args:
        host: Bare hostname to match.
        provider: The profile to register.
    """
    PROVIDERS[host.lower()] = provider


def seed_limits(host: str, authenticated: bool = False) -> Dict[str, List[SeedLimit]]:
    """
    Documented limits for a host, as scope suffix -> limits.

    Args:
        host: Bare hostname.
        authenticated: Whether requests to this host carry credentials. Limits
            usually differ, often by orders of magnitude, and nothing in a
            response tells you which side you are on before you send one.

    Returns:
        Scope suffixes mapped to their seed limits; empty if the host is unknown.
    """
    provider = get_provider(host)
    if provider is None:
        return {}

    if authenticated and provider.authenticated_scopes:
        return provider.authenticated_scopes

    return provider.scopes
