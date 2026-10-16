"""Mutation model for the oracle: what changed in the tenant, and when intent changed.

A Mutation has a type, parameters, and `confirmed_at`: the time (any monotone clock the caller
uses, usually seconds since the run started) at which Entra/REST CONFIRMED the change. The
oracle's INTENT changes at `confirmed_at`, not when the change was requested and not when
SharePoint starts enforcing it. A mutation with confirmed_at=None is not confirmed yet and has
no effect on intent.

Targets are written as: "root" (site-root grants), a folder path such as "Engineering", or
"special:<file id>" (the key form used by spec.special_key()).

This module must not import engine code (see tests/test_oracle_isolation.py).
"""

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, model_validator

ROOT_TARGET = "root"
SPECIAL_PREFIX = "special:"

# Principal kinds the oracle understands. Anything else is denied (and logged): fail closed.
KNOWN_KINDS: frozenset[str] = frozenset({"user", "entra_group"})

# Roles that confer READ access (verified: Read, Full Control (Graph "owner"), Edit (Graph
# "write") can read). "limited_access" is deliberately absent: it is not read access
# (docs/FINDINGS.md, "Limited Access"). Unknown roles are not read access either.
READ_ROLES: frozenset[str] = frozenset({"read", "edit", "full_control"})


class OracleError(ValueError):
    """A mutation or lookup the oracle cannot interpret. Raised, never ignored (fail closed)."""


class MutationType(StrEnum):
    GRANT_ADDED = "grant_added"
    GRANT_REMOVED = "grant_removed"
    GROUP_MEMBER_ADDED = "group_member_added"
    GROUP_MEMBER_REMOVED = "group_member_removed"
    INHERITANCE_BROKEN = "inheritance_broken"
    INHERITANCE_RESTORED = "inheritance_restored"
    FILE_MOVED = "file_moved"


# type -> (required parameters, optional parameters)
PARAMS: dict[MutationType, tuple[frozenset[str], frozenset[str]]] = {
    MutationType.GRANT_ADDED: (
        frozenset({"target", "principal_kind", "principal_ref", "role"}),
        frozenset(),
    ),
    MutationType.GRANT_REMOVED: (
        frozenset({"target", "principal_kind", "principal_ref"}),
        frozenset({"role"}),  # omitted: every grant of that principal on the target is removed
    ),
    MutationType.GROUP_MEMBER_ADDED: (
        frozenset({"group", "member_kind", "member_ref"}),
        frozenset(),
    ),
    MutationType.GROUP_MEMBER_REMOVED: (
        frozenset({"group", "member_kind", "member_ref"}),
        frozenset(),
    ),
    MutationType.INHERITANCE_BROKEN: (
        frozenset({"target"}),
        frozenset({"copy_role_assignments"}),  # default False: the item starts with NO grants
    ),
    MutationType.INHERITANCE_RESTORED: (frozenset({"target"}), frozenset()),
    MutationType.FILE_MOVED: (frozenset({"file", "to_folder"}), frozenset()),
}


class Mutation(BaseModel):
    """One change to the tenant. Immutable. Parameters are checked against the type."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: MutationType
    params: dict[str, Any]
    confirmed_at: float | None = None

    @model_validator(mode="after")
    def _check_params(self) -> "Mutation":
        required, optional = PARAMS[self.type]
        given = set(self.params)
        missing = required - given
        unknown = given - required - optional
        if missing or unknown:
            raise ValueError(
                f"{self.type.value}: missing parameters {sorted(missing)}, "
                f"unknown parameters {sorted(unknown)}"
            )
        return self
