"""Naive-UTC clock helpers.

Every timestamp in this library is a *naive* datetime in UTC. Mixing naive and
aware datetimes raises ``TypeError`` on comparison, so the rule is enforced in
one place rather than at 20-odd call sites.

``datetime.utcnow()`` and ``datetime.utcfromtimestamp()`` produce exactly those
values but are deprecated from Python 3.12 and slated for removal. These
wrappers keep the semantics and drop the deprecation.
"""

from datetime import datetime, timezone


def utcnow() -> datetime:
    """Current UTC time, as a naive datetime."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def utcfromtimestamp(timestamp: float) -> datetime:
    """Convert a Unix timestamp to a naive UTC datetime."""
    return datetime.fromtimestamp(timestamp, timezone.utc).replace(tzinfo=None)


def to_epoch(dt: datetime) -> float:
    """Convert a naive UTC datetime to a Unix timestamp."""
    return dt.replace(tzinfo=timezone.utc).timestamp()
