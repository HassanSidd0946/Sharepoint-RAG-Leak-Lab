"""The read side of the state: may this user read this document, according to the stored state?

A decision combines the two stores: the document's scope (nearest ancestor with its own role
assignments, else the site root), that scope's grants, and the user's transitive groups. A grant
matches when it is a read role AND names the user's UPN or one of the user's groups.

Fail closed (CLAUDE.md rule 3): the answer is DENY, with a reason, when
  - the ACL poller has never succeeded or its last success is older than `max_age_s` (stale_acl):
    nothing can be trusted, direct grants included, because they come from the same stored state,
  - the membership poller is stale: every GROUP-based grant is denied (the user's groups are not
    known). A grant that names the user directly still counts. If a group grant is all that is left
    the reason is stale_membership,
  - the document is unknown (no_such_document) or the user is unknown to the directory and in no
    group (unknown_principal),
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
    via: str | None = None  # what allowed it: "user:<upn>" or "group:<group id>"


@dataclass(frozen=True)
class Freshness:
    membership_age_s: float | None  # None: never succeeded
    acl_age_s: float | None
    max_age_s: float

    @property
    def membership_fresh(self) -> bool:
        return self.membership_age_s is not None and self.membership_age_s <= self.max_age_s

    @property
    def acl_fresh(self) -> bool:
        return self.acl_age_s is not None and self.acl_age_s <= self.max_age_s

    @property
    def fresh(self) -> bool:
        return self.membership_fresh and self.acl_fresh


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
        return self.decide_with(user_id, doc_id, self.freshness())

    def decide_with(self, user_id: str, doc_id: str, fresh: Freshness) -> Decision:
        """decide() against a freshness reading the caller already took (one per request)."""
        if not fresh.acl_fresh:
            return Decision(False, "stale_acl")
        scope = self._repo.scope_of_item(doc_id)
        if scope is None:
            return Decision(False, "no_such_document")
        grants = self._repo.grants_of_scope(scope)
        upn = self._repo.upn_of(user_id)
        groups = self._repo.groups_of(user_id) if fresh.membership_fresh else set()
        via = _match(upn, groups, grants)
        if via is not None:
            return Decision(True, "allowed", via)
        if not fresh.membership_fresh and any(
            g.kind == "entra_group" and g.grants_read for g in grants
        ):
            return Decision(False, "stale_membership")
        if upn is None and not groups:
            return Decision(False, "unknown_principal")
        return Decision(False, "no_matching_grant")

    def can_read(self, user_id: str, doc_id: str) -> bool:
        return self.decide(user_id, doc_id).allowed

    def readable(self, user_id: str) -> set[str]:
        """Every FILE the user may read (group grants only while the membership state is fresh;
        nothing at all while the ACL state is stale)."""
        fresh = self.freshness()
        if not fresh.acl_fresh:
            return set()
        snapshot = self._repo.load_acl()
        groups = self._repo.groups_of(user_id) if fresh.membership_fresh else set()
        upn = self._repo.upn_of(user_id)
        return {
            item.doc_id
            for item in snapshot.items.values()
            if not item.is_folder
            and _match(upn, groups, snapshot.grants.get(snapshot.scope_of(item.doc_id), ()))
            is not None
        }


def _match(upn: str | None, groups: set[str], grants: tuple[Grant, ...]) -> str | None:
    """What lets the user in ("user:<upn>" / "group:<id>"), or None. Unknown grants never match."""
    for g in grants:
        if not g.grants_read:
            continue
        if g.kind == "user" and upn is not None and g.ref == upn:
            return f"user:{upn}"
    for g in grants:
        if g.grants_read and g.kind == "entra_group" and g.ref in groups:
            return f"group:{g.ref}"
    return None
