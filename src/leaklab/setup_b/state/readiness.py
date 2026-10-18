"""Is Setup B's state ready to be measured? And what did the first sync look like?

READY means: BOTH pollers have completed two polls in a row that found nothing to change, AND one
full ACL verification has completed (and, if it found drift, two quiet polls have followed it).
Any poll that fails or changes something resets that poller's streak. It is a statement about
the state's convergence after start-up, not about freshness: the reader's 30 s rule
(state/access.py) is unchanged and keeps deciding what is denied.

Why it exists (docs/FINDINGS.md, "Benchmark, first full run"): right after a fresh tenant was
generated, Setup B withheld documents it should have served for 6-98 s although both pollers were
"fresh". The first sync had been incomplete and later polls or the 60 s verification completed it.
The runner waits for /ready before its baseline, and `StartupLog` records what the first sync saw
against what a later poll added, so the hypothesis can be checked from data.

Thread-safe: the membership, ACL and verification loops call into it from different threads.
"""

import threading
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, dataclass

from leaklab.setup_b.state.repository import StateRepository

QUIET_POLLS_NEEDED = 2
MEMBERSHIP = "membership"
ACL = "acl"


@dataclass(frozen=True)
class ReadinessState:
    ready: bool
    quiet_polls: dict[str, int]
    verifications: int
    needed_quiet_polls: int = QUIET_POLLS_NEEDED

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


class Readiness:
    def __init__(self, needed_quiet_polls: int = QUIET_POLLS_NEEDED) -> None:
        self._needed = needed_quiet_polls
        self._lock = threading.Lock()
        self._quiet = {MEMBERSHIP: 0, ACL: 0}
        self._verifications = 0

    def record_poll(self, source: str, *, ok: bool, changed: bool) -> None:
        with self._lock:
            self._quiet[source] = self._quiet.get(source, 0) + 1 if ok and not changed else 0

    def record_verification(self, *, ok: bool, changed: bool) -> None:
        """A verification that completed counts; one that found drift also changed the state."""
        with self._lock:
            if not ok:
                return
            self._verifications += 1
            if changed:
                self._quiet[ACL] = 0

    def state(self) -> ReadinessState:
        with self._lock:
            quiet = dict(self._quiet)
            verified = self._verifications
        ready = verified >= 1 and all(n >= self._needed for n in quiet.values())
        return ReadinessState(ready, quiet, verified, self._needed)


def snapshot(repo: StateRepository) -> dict[str, int]:
    """What the state holds: the numbers a complete sync would reproduce."""
    acl = repo.load_acl()
    return {
        "membership_edges": len(repo.load_edges()),
        "directory_users": len(repo.load_directory()),
        "acl_items": len(acl.items),
        "acl_files": sum(1 for i in acl.items.values() if not i.is_folder),
        "unique_scopes": sum(1 for i in acl.items.values() if i.unique),
        "scopes_with_grants": len(acl.grants),
        "grants": sum(len(g) for g in acl.grants.values()),
        "read_grants": sum(1 for g in acl.grants.values() for x in g if x.grants_read),
    }


class StartupLog:
    """The first sync's numbers, and (on request) how the state differs from them later."""

    def __init__(self, repo: StateRepository, clock: Callable[[], float]) -> None:
        self._repo = repo
        self._clock = clock
        self.first: dict[str, int] | None = None
        self._first_events = 0
        self.first_at: float | None = None

    def first_sync_done(self) -> dict[str, int]:
        self.first = snapshot(self._repo)
        self._first_events = len(self._repo.events())
        self.first_at = self._clock()
        return self.first

    def report(self, ready_at: float | None) -> dict[str, object]:
        """first sync vs now, what changed in between, and who found it."""
        now = snapshot(self._repo)
        events = self._repo.events()[self._first_events :]
        found = Counter(f"{e.source}:{e.kind}:{e.detail.get('found_by', 'poll')}" for e in events)
        first = self.first or {}
        return {
            "first_sync": first,
            "now": now,
            "difference": {k: now[k] - first.get(k, 0) for k in now if now[k] != first.get(k, 0)},
            "events_after_first_sync": dict(found),
            "found_by_verify": sum(n for k, n in found.items() if k.endswith(":verify")),
            "first_sync_at": self.first_at,
            "ready_at": ready_at,
            "complete_at_first_sync": all(now[k] == first.get(k, 0) for k in now),
        }
