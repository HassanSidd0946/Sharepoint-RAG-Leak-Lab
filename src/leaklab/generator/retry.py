"""Retry rules for the behaviors verified in docs/FINDINGS.md. Nothing is retried that was not seen.

  - Adding a member to a brand-new Entra group can answer HTTP 404 for 5-10s:
    retry Graph 404 on member adds, up to MEMBER_ADD_TIMEOUT_S.
  - SharePoint `ensureuser` on a brand-new group (or user) can answer HTTP 400 "could not be found"
    for a variable time (seen: from none to 20s+): retry that one error, up to ENSUREUSER_TIMEOUT_S
    (the 2 minutes used by the probes).
  - Graph 400 "already exist" on a member add is SUCCESS (GraphClient.add_*_to_group already returns
    False for it) and a 404 on a delete means "already gone": see gone_ok().

The sleep and clock are injectable so tests need no real waiting.
"""

import json
import time
from collections.abc import Callable
from typing import TypeVar

from leaklab.graph_client import GraphError
from leaklab.sp_rest import SPRestError

T = TypeVar("T")

MEMBER_ADD_TIMEOUT_S = 60.0
ENSUREUSER_TIMEOUT_S = 120.0
RETRY_INTERVAL_S = 5.0


def is_member_add_not_ready(exc: Exception) -> bool:
    return isinstance(exc, GraphError) and exc.status_code == 404


def is_ensureuser_not_found(exc: Exception) -> bool:
    return (
        isinstance(exc, SPRestError)
        and exc.status_code == 400
        and "could not be found" in json.dumps(exc.body).lower()
    )


def retry_call[T](
    op: Callable[[], T],
    *,
    retryable: Callable[[Exception], bool],
    timeout_s: float,
    interval_s: float = RETRY_INTERVAL_S,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> T:
    """Call `op`; on a retryable error wait and try again until `timeout_s` has passed.

    Any other error, and the last retryable error once the time is up, is raised unchanged.
    """
    start = clock()
    while True:
        try:
            return op()
        except Exception as exc:
            if not retryable(exc) or clock() - start + interval_s > timeout_s:
                raise
            sleep(interval_s)


def gone_ok(op: Callable[[], object]) -> bool:
    """Run a delete. True if it deleted something, False if it was already gone (HTTP 404)."""
    try:
        op()
    except GraphError as exc:
        if exc.status_code == 404:
            return False
        raise
    return True
