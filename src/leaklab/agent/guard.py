"""The GUARDED mode's access check: may THIS user read EVERY source of this thing, right now?

A cache entry, a memory item and every piece of text about to reach the LLM carries the sources it
was made from. The guard asks Setup B's authoritative state (the membership and ACL stores through
StateReader), never the index's stored ACL fields, whether the CURRENT user may read each source.
Fail closed (CLAUDE.md rule 3): the answer is DENY when

  * the thing has no sources recorded, or an empty list (unknown provenance),
  * a source belongs to another tenant generation than the live one,
  * any single source is not allowed (one failure is enough),
  * the state cannot say (stale pollers, unknown document, unknown user) or the check raises.

check_sources returns the per-source results so the journal can show why.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from leaklab.agent.types import SourceRef
from leaklab.setup_b.state.access import StateReader


@dataclass(frozen=True)
class AccessResult:
    item_id: str
    allowed: bool
    reason: str


class AccessChecker(Protocol):
    generation: str  # the live tenant generation

    def check(self, user_oid: str, item_ids: Sequence[str]) -> list[AccessResult]: ...


class StateAccess:
    """Access from Setup B's state: one freshness reading per call, then one decision per item."""

    def __init__(self, reader: StateReader, generation: str) -> None:
        self._reader = reader
        self.generation = generation

    def check(self, user_oid: str, item_ids: Sequence[str]) -> list[AccessResult]:
        try:
            fresh = self._reader.freshness()
        except Exception as exc:
            return [AccessResult(i, False, f"error:{type(exc).__name__}") for i in item_ids]
        results: list[AccessResult] = []
        for item in item_ids:
            try:
                decision = self._reader.decide_with(user_oid, item, fresh)
                results.append(AccessResult(item, decision.allowed, decision.reason))
            except Exception as exc:  # unknown is a denial
                results.append(AccessResult(item, False, f"error:{type(exc).__name__}"))
        return results


@dataclass(frozen=True)
class GuardVerdict:
    allowed: bool
    reason: str
    results: tuple[AccessResult, ...] = ()


def check_sources(
    access: AccessChecker, user_oid: str, sources: tuple[SourceRef, ...] | None
) -> GuardVerdict:
    """May the user read every one of `sources`? Anything but a clear yes for ALL is a no."""
    if not sources:
        return GuardVerdict(False, "no_sources")
    if any(s.generation != access.generation for s in sources):
        return GuardVerdict(False, "generation_mismatch")
    try:
        results = access.check(user_oid, [s.item_id for s in sources])
    except Exception as exc:
        return GuardVerdict(False, f"error:{type(exc).__name__}")
    if len(results) != len(sources):
        return GuardVerdict(False, "incomplete_check", tuple(results))
    denied = [r for r in results if not r.allowed]
    if denied:
        return GuardVerdict(False, f"denied:{denied[0].reason}", tuple(results))
    return GuardVerdict(True, "allowed", tuple(results))
