"""membership_store: who is in which group, kept current from Graph's GROUP DELTA.

A poll (every 5 s) asks the GroupDeltaSource for what changed since the saved link and applies it to
the direct-membership edges. The transitive closure (user -> every group containing the user,
through nesting) is recomputed and stored with the edges, so a read is one indexed lookup.

Every poll is one atomic commit: edges + closure + directory + event log + poller state. A poll that
FAILS leaves the data alone, records the error and does NOT move last_success_at, so a reader can
tell how stale the data is (access.StateReader refuses stale data: fail closed).

Event log: each edge that appeared or disappeared becomes one event with `seen_at` (the clock when
the poll returned) and `applied_at` (the clock just before the commit).

Directory: Graph's group delta gives member ids only. User grants from SharePoint carry a UPN, so
the ids of users seen in groups are resolved to UPNs through a DirectorySource. An id that cannot be
resolved simply has no UPN: user grants never match it (deny), group grants still work.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from leaklab.setup_b.state.closure import transitive_groups
from leaklab.setup_b.state.directory import DirectoryStore
from leaklab.setup_b.state.model import (
    Edge,
    Event,
    GroupPage,
    MembershipChange,
    PollerState,
    ResyncRequired,
)
from leaklab.setup_b.state.repository import StateRepository

NAME = "membership"


class GroupDeltaSource(Protocol):
    def fetch(self, link: str | None) -> GroupPage:
        """Changes since `link` (None: the complete current state). Raises ResyncRequired on 410."""
        ...


@dataclass(frozen=True)
class PollResult:
    ok: bool
    changes: int = 0
    error: str | None = None
    full: bool = False


class MembershipStore:
    def __init__(
        self,
        repo: StateRepository,
        source: GroupDeltaSource,
        directory: DirectoryStore,
        clock: Callable[[], float],
    ) -> None:
        self._repo = repo
        self._source = source
        self._directory = directory
        self._clock = clock

    def poll_once(self) -> PollResult:
        """One poll. Never raises for a source problem: it is recorded and reported."""
        attempt = self._clock()
        state = self._repo.get_state(NAME)
        link = state.link if state else None
        try:
            try:
                page = self._source.fetch(link)
            except ResyncRequired:
                page = self._source.fetch(None)  # the saved link is dead: enumerate everything
            seen_at = self._clock()
            return self._apply(page, seen_at, attempt)
        except Exception as exc:  # a poll failure must not stop the loop, and must be visible
            error = f"{type(exc).__name__}: {exc}"
            old = self._repo.get_state(NAME)
            self._repo.set_state(
                PollerState(
                    NAME,
                    old.link if old else None,
                    old.last_success_at if old else None,
                    attempt,
                    error,
                )
            )
            return PollResult(ok=False, error=error)

    # -- applying one page ------------------------------------------------------------------------

    def _apply(self, page: GroupPage, seen_at: float, attempt: float) -> PollResult:
        before = self._repo.load_edges()
        after = apply_changes(before, page.changes, full=page.full)
        closure = transitive_groups(after)
        applied_at = self._clock()
        events = [
            Event("membership", "member_added", _subject(e), _detail(e), seen_at, applied_at)
            for e in sorted(after - before, key=_subject)
        ] + [
            Event("membership", "member_removed", _subject(e), _detail(e), seen_at, applied_at)
            for e in sorted(before - after, key=_subject)
        ]
        with self._repo.atomic():
            if after != before or page.full:
                self._repo.save_edges(after, closure)
            self._directory.refresh(self._repo)
            self._repo.append_events(events)
            self._repo.set_state(PollerState(NAME, page.link, applied_at, attempt, None))
        return PollResult(ok=True, changes=len(events), full=page.full)


def apply_changes(
    edges: set[Edge], changes: Sequence[MembershipChange], *, full: bool
) -> set[Edge]:
    """The edge set after `changes`. A full enumeration replaces everything; a delta edits it."""
    result: set[Edge] = set() if full else set(edges)
    for change in changes:
        if change.deleted:
            result = {
                e
                for e in result
                if e.group_id != change.group_id
                and not (e.member_kind == "group" and e.member_id == change.group_id)
            }
            continue
        for kind, member in change.removed:
            result.discard(Edge(change.group_id, kind, member))
        for kind, member in change.added:
            result.add(Edge(change.group_id, kind, member))
    return result


def _subject(edge: Edge) -> str:
    return f"{edge.group_id}:{edge.member_kind}:{edge.member_id}"


def _detail(edge: Edge) -> dict[str, str]:
    return {"group": edge.group_id, "member_kind": edge.member_kind, "member": edge.member_id}
