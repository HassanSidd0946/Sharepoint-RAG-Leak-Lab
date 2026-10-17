"""The read side of the state: may this user read this document, according to the stored state?

A decision combines the two stores: the document's scope (nearest ancestor with its own role
assignments, else the site root), that scope's grants, and the user's transitive groups. A grant
matches when it is a read role AND names the user's UPN or one of the user's groups.

Fail closed (CLAUDE.md rule 3): the answer is DENY, with a reason, when
  - either poller has never succeeded or its last success is older than `max_age_s`
    (stale_membership / stale_acl),
  - the document is unknown (no_such_document),
  - no grant matches (no_matching_grant). A grant of kind "unknown" (a special claim, an Entra group
    inside a SharePoint group, a role we do not know) never matches.
Nothing here calls Graph or SharePoint: it reads the repository only.
"""

from collections.abc import Callable
from dataclasses import dataclass

from leaklab.setup_b.state import acl, membership
from leaklab.setup_b.state.model import Grant
from leaklab.setup_b.state.repository import StateRepository


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str


@dataclass(frozen=True)
class Freshness:
    membership_age_s: float | None  # None: never succeeded
    acl_age_s: float | None
    max_age_s: float

    @property
    def fresh(self) -> bool:
        ages = (self.membership_age_s, self.acl_age_s)
        return all(a is not None and a <= self.max_age_s for a in ages)


class StateReader:
    def __init__(
        self, repo: StateRepository, clock: Callable[[], float], max_age_s: float = 30.0
    ) -> None:
        self._repo = repo
        self._clock = clock
        self._max_age_s = max_age_s

    def freshness(self) -> Freshness:
        now = self._clock()

        def age(name: str) -> float | None:
            state = self._repo.get_state(name)
            return (
                None
                if state is None or state.last_success_at is None
                else now - state.last_success_at
            )

        return Freshness(age(membership.NAME), age(acl.NAME), self._max_age_s)

    def decide(self, user_id: str, doc_id: str) -> Decision:
        fresh = self.freshness()
        if fresh.membership_age_s is None or fresh.membership_age_s > fresh.max_age_s:
            return Decision(False, "stale_membership")
        if fresh.acl_age_s is None or fresh.acl_age_s > fresh.max_age_s:
            return Decision(False, "stale_acl")
        scope = self._repo.scope_of_item(doc_id)
        if scope is None:
            return Decision(False, "no_such_document")
        if self._matches(user_id, self._repo.grants_of_scope(scope)):
            return Decision(True, "allowed")
        return Decision(False, "no_matching_grant")

    def can_read(self, user_id: str, doc_id: str) -> bool:
        return self.decide(user_id, doc_id).allowed

    def readable(self, user_id: str) -> set[str]:
        """Every FILE the user may read (empty when the state is stale)."""
        if not self.freshness().fresh:
            return set()
        snapshot = self._repo.load_acl()
        groups = self._repo.groups_of(user_id)
        upn = self._repo.upn_of(user_id)
        return {
            item.doc_id
            for item in snapshot.items.values()
            if not item.is_folder
            and _matches(upn, groups, snapshot.grants.get(snapshot.scope_of(item.doc_id), ()))
        }

    def _matches(self, user_id: str, grants: tuple[Grant, ...]) -> bool:
        return _matches(self._repo.upn_of(user_id), self._repo.groups_of(user_id), grants)


def _matches(upn: str | None, groups: set[str], grants: tuple[Grant, ...]) -> bool:
    for g in grants:
        if not g.grants_read:
            continue
        if g.kind == "user" and upn is not None and g.ref == upn:
            return True
        if g.kind == "entra_group" and g.ref in groups:
            return True
    return False
