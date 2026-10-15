"""Retry policy shared by GraphClient and SharePointRest.

Retry on HTTP 429/503/504, at most MAX_ATTEMPTS attempts in total. The wait is the
Retry-After header if present (capped at MAX_RETRY_AFTER seconds), else exponential backoff
with jitter. Nothing else is retried: retrying a timed-out POST could create a duplicate.
"""

import random
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx

RETRY_STATUSES: frozenset[int] = frozenset({429, 503, 504})
MAX_ATTEMPTS = 5
BASE_DELAY = 1.0
MAX_RETRY_AFTER = 120.0


def parse_retry_after(value: str | None) -> float | None:
    """Parse a Retry-After header (delta-seconds or HTTP date) into seconds, or None."""
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - datetime.now(UTC)).total_seconds())


def retry_delay(resp: httpx.Response, attempt: int, rng: random.Random) -> float:
    """Seconds to wait before retry number `attempt` (1-based) of a failed response."""
    retry_after = parse_retry_after(resp.headers.get("Retry-After"))
    if retry_after is not None:
        return min(retry_after, MAX_RETRY_AFTER)
    return float(BASE_DELAY * 2 ** (attempt - 1) * rng.uniform(0.5, 1.5))
