"""Plain data the state stores exchange: group changes, drive items, grants, events.

Identities: users and groups are Entra OBJECT IDS (as Graph reports them); a user grant read from
SharePoint carries a login claim, which is reduced to the lower-case UPN and matched to an object id
through the directory (users table). Nothing here knows about the oracle or the tenant spec.
"""

from dataclasses import dataclass, field
from typing import Any, Literal

MemberKind = Literal["user", "group"]
GrantKind = Literal["user", "entra_group", "unknown"]

ROOT_SCOPE = "root"  # the site root (/web): the scope every top-level item inherits from

# Role names that confer READ access. Verified 2026-10-15/16 (docs/FINDINGS.md): Read, Edit and Full
# Control can read; "Limited Access" cannot. Any other role name is NOT read access (fail closed).
READ_ROLES: frozenset[str] = frozenset({"Read", "Edit", "Full Control"})


class ResyncRequired(Exception):
    """The source cannot continue from the saved link (Graph HTTP 410): start a full enumeration."""


@dataclass(frozen=True)
class Edge:
    """`member` is directly inside `group`."""

    group_id: str
    member_kind: MemberKind
    member_id: str


@dataclass(frozen=True)
class MembershipChange:
    """What the group delta says about one group. In a full enumeration `added` is the whole
    membership; in a delta it is the members added and `removed` the members removed."""

    group_id: str
    display_name: str | None = None
    deleted: bool = False
    added: tuple[tuple[MemberKind, str], ...] = ()
    removed: tuple[tuple[MemberKind, str], ...] = ()


@dataclass(frozen=True)
class GroupPage:
    changes: tuple[MembershipChange, ...]
    link: str  # pass it to the next fetch
    full: bool  # True: `changes` is the complete state (replace everything)


@dataclass(frozen=True)
class DriveItem:
    """An item of the document library as the drive delta reports it."""

    item_id: str
    name: str
    parent_id: str | None
    is_folder: bool
    deleted: bool = False
    shared_changed: bool = False  # @microsoft.graph.sharedChanged (needs the three Prefer headers)
    web_url: str | None = None


@dataclass(frozen=True)
class DrivePage:
    items: tuple[DriveItem, ...]
    link: str
    full: bool


@dataclass(frozen=True)
class RawAssignment:
    """One role assignment as SharePoint REST reports it."""

    login: str
    principal_id: int
    principal_type: int  # SharePoint PrincipalType: 1 user, 4 security group, 8 SharePoint group
    roles: tuple[str, ...]  # role definition names (Limited Access included)


@dataclass(frozen=True)
class Grant:
    """One (principal, role) of a scope, after SharePoint groups have been expanded.

    kind "user": ref is a lower-case UPN. "entra_group": ref is the group's object id. "unknown":
    anything we cannot interpret (special claims, an Entra group inside a SharePoint group): it is
    kept for audit and NEVER grants access. `via` says how it got here: "direct" or "sp_group:<id>".
    """

    kind: GrantKind
    ref: str
    role: str
    via: str = "direct"

    @property
    def grants_read(self) -> bool:
        return self.kind != "unknown" and self.role in READ_ROLES


@dataclass(frozen=True)
class DocRecord:
    doc_id: str
    name: str
    parent_id: str | None
    is_folder: bool
    web_url: str | None
    unique: bool  # has its own role assignments (is a scope of its own)


@dataclass
class AclSnapshot:
    """The ACL store's whole state: the item tree and the grants of every scope."""

    items: dict[str, DocRecord] = field(default_factory=dict)
    grants: dict[str, tuple[Grant, ...]] = field(default_factory=dict)  # scope id -> grants

    def scope_of(self, item_id: str) -> str:
        """The nearest ancestor-or-self with its own permissions, else the site root."""
        seen: set[str] = set()
        current: str | None = item_id
        while current is not None and current not in seen:
            seen.add(current)
            item = self.items.get(current)
            if item is None:
                return ROOT_SCOPE
            if item.unique:
                return current
            current = item.parent_id
        return ROOT_SCOPE


@dataclass(frozen=True)
class Event:
    """One change the pollers saw and applied. `seen_at` is when the poll that revealed it returned;
    `applied_at` is when the store had committed it (epoch seconds, the mutation log's clock)."""

    source: str  # "membership" | "acl"
    kind: str
    subject: str
    detail: dict[str, Any]
    seen_at: float
    applied_at: float


@dataclass(frozen=True)
class PollerState:
    name: str
    link: str | None
    last_success_at: float | None
    last_attempt_at: float | None
    last_error: str | None
