"""Expected access: who should be able to read which canary, from the spec and mutations only.

Inputs: a TenantSpec, a list of Mutation, and a time. Output: persona id -> set of canary strings
the persona should be able to read at that time. Nothing else is consulted: no Graph, no
SharePoint, no cache, no observation of what a system actually returned.

Rules implemented (each is verified in docs/FINDINGS.md, "Oracle rules supported by verified
results"):
  1. A grant to an Entra group reaches the group's members, however deeply nested. A grant to a
     SharePoint group reaches the users it contains directly (an Entra group inside a SharePoint
     group is not modelled: not verified, so a mutation asking for it raises OracleError). The
     oracle gives the same answer for every grant type; how fast a system follows a change of each
     type is what scoring measures.
  2. An item with unique permissions (inherits: false) has exactly its own grants.
  3. An item that inherits has its parent's effective grants; a top-level folder inherits the
     site-root grants (so root grants reach every inheriting item).
  4. A moved special file keeps its own grants (and, if it has none of its own, follows the
     folder it now sits in).
  5. "Limited Access" and unknown roles are not read access.
  6. Site admins are NOT evaluated: only users with persona: true appear in the result. How an
     admin should be treated is an open decision (the admin keeps access in ways the grants do
     not explain), so it is excluded, not guessed.
Anything the oracle cannot interpret (unknown principal kind, unknown folder, an invalid mutation)
means DENY or an OracleError, never access.

Time semantics: intent changes at a mutation's `confirmed_at`. At time t every mutation with
confirmed_at <= t has been applied, in (confirmed_at, list position) order; a mutation with
confirmed_at None is not applied. This module says nothing about when a system SHOULD have caught
up; that is measured by the scoring module.

This module must not import engine code (see tests/test_oracle_isolation.py).
"""

import logging
from bisect import bisect_right
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from leaklab.oracle.model import (
    KNOWN_KINDS,
    READ_ROLES,
    ROOT_TARGET,
    SPECIAL_PREFIX,
    Mutation,
    MutationType,
    OracleError,
)
from leaklab.spec import TenantSpec, generate_canaries, special_key

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class _Grant:
    kind: str
    ref: str
    role: str


@dataclass
class _Scope:
    """Permissions of one item: either inherits (grants unused) or has exactly `grants`."""

    inherits: bool
    grants: list[_Grant] = field(default_factory=list)


@dataclass
class _File:
    folder: str
    scope: _Scope


@dataclass
class _State:
    root: _Scope
    folders: dict[str, _Scope]
    files: dict[str, _File]
    members: dict[str, set[tuple[str, str]]]  # group id -> {(kind, ref)} of its direct members
    sp_members: dict[str, set[str]]  # SharePoint group id -> user ids it contains
    users: set[str]


def _grant(kind: str, ref: str, role: str) -> _Grant:
    if kind not in KNOWN_KINDS:
        log.warning("unknown principal kind %r (ref %r): it will never match, i.e. deny", kind, ref)
    return _Grant(kind, ref, role)


def _build_state(spec: TenantSpec) -> _State:
    root = _Scope(
        False, [_grant(g.principal.kind, g.principal.ref, g.role) for g in spec.site.root_grants]
    )
    folders = {
        f.path: _Scope(
            f.inherits, [_grant(g.principal.kind, g.principal.ref, g.role) for g in f.grants]
        )
        for f in spec.folders
    }
    files = {
        sf.id: _File(
            sf.folder,
            _Scope(
                sf.inherits, [_grant(g.principal.kind, g.principal.ref, g.role) for g in sf.grants]
            ),
        )
        for sf in spec.special_files
    }
    members: dict[str, set[tuple[str, str]]] = {}
    for grp in spec.groups:
        entries = {("user", m) for m in grp.members} | {
            ("entra_group", m) for m in grp.member_groups
        }
        members[grp.id] = entries
    sp_members = {sg.id: set(sg.members) for sg in spec.sp_groups}
    return _State(root, folders, files, members, sp_members, {u.id for u in spec.users})


def _scope(state: _State, target: str) -> _Scope:
    if target == ROOT_TARGET:
        return state.root
    if target.startswith(SPECIAL_PREFIX):
        file = state.files.get(target[len(SPECIAL_PREFIX) :])
        if file is None:
            raise OracleError(f"unknown special file in target {target!r}")
        return file.scope
    folder = state.folders.get(target)
    if folder is None:
        raise OracleError(f"unknown folder in target {target!r}")
    return folder


def _folder_grants(state: _State, path: str) -> list[_Grant]:
    """Effective grants of a folder: its own if unique, else its parent's, up to the root."""
    while True:
        scope = state.folders.get(path)
        if scope is None:
            raise OracleError(f"unknown folder {path!r}")
        if not scope.inherits:
            return scope.grants
        parent = path.rpartition("/")[0]
        if not parent:
            return state.root.grants
        path = parent


def _parent_grants(state: _State, target: str) -> list[_Grant]:
    """The grants an item would inherit right now (used when inheritance is broken with a copy)."""
    if target.startswith(SPECIAL_PREFIX):
        file = state.files.get(target[len(SPECIAL_PREFIX) :])
        if file is None:
            raise OracleError(f"unknown special file in target {target!r}")
        return _folder_grants(state, file.folder)
    parent = target.rpartition("/")[0]
    return _folder_grants(state, parent) if parent else state.root.grants


def _principals(state: _State, user: str) -> set[tuple[str, str]]:
    """The user plus every group that contains the user, directly or through nesting."""
    groups: set[str] = set()
    frontier = [g for g, members in state.members.items() if ("user", user) in members]
    while frontier:
        group = frontier.pop()
        if group in groups:  # also stops cycles introduced by a mutation
            continue
        groups.add(group)
        frontier.extend(
            g for g, members in state.members.items() if ("entra_group", group) in members
        )
    sp_groups = {g for g, members in state.sp_members.items() if user in members}
    return (
        {("user", user)}
        | {("entra_group", g) for g in groups}
        | {("sp_group", g) for g in sp_groups}
    )


def _can_read(grants: Sequence[_Grant], principals: set[tuple[str, str]]) -> bool:
    return any(g.role in READ_ROLES and (g.kind, g.ref) in principals for g in grants)


def _require_unique(scope: _Scope, target: str) -> None:
    if scope.inherits:
        raise OracleError(
            f"{target!r} inherits its permissions: break inheritance before changing grants"
        )


def _apply(state: _State, mutation: Mutation) -> None:
    p = mutation.params
    kind = mutation.type
    if kind is MutationType.GRANT_ADDED:
        scope = _scope(state, str(p["target"]))
        _require_unique(scope, str(p["target"]))
        new = _grant(str(p["principal_kind"]), str(p["principal_ref"]), str(p["role"]))
        if new not in scope.grants:
            scope.grants.append(new)
    elif kind is MutationType.GRANT_REMOVED:
        scope = _scope(state, str(p["target"]))
        _require_unique(scope, str(p["target"]))
        role = p.get("role")
        scope.grants[:] = [
            g
            for g in scope.grants
            if not (
                g.kind == p["principal_kind"]
                and g.ref == p["principal_ref"]
                and (role is None or g.role == role)
            )
        ]
    elif kind in (MutationType.GROUP_MEMBER_ADDED, MutationType.GROUP_MEMBER_REMOVED):
        group = str(p["group"])
        member_kind, member_ref = str(p["member_kind"]), str(p["member_ref"])
        if group in state.sp_members:
            if member_kind != "user":
                raise OracleError(
                    f"SharePoint group {group!r} can only contain users (got {member_kind!r}): "
                    "an Entra group inside a SharePoint group is not verified"
                )
            if member_ref not in state.users:
                raise OracleError(f"unknown user {member_ref!r}")
            if kind is MutationType.GROUP_MEMBER_ADDED:
                state.sp_members[group].add(member_ref)
            else:
                state.sp_members[group].discard(member_ref)
            return
        if group not in state.members:
            raise OracleError(f"unknown group {group!r}")
        if member_kind not in ("user", "entra_group"):
            raise OracleError(f"unknown member kind {member_kind!r}")
        known = state.users if member_kind == "user" else set(state.members)
        if member_ref not in known:
            raise OracleError(f"unknown {member_kind} {member_ref!r}")
        if kind is MutationType.GROUP_MEMBER_ADDED:
            state.members[group].add((member_kind, member_ref))
        else:
            state.members[group].discard((member_kind, member_ref))
    elif kind is MutationType.INHERITANCE_BROKEN:
        target = str(p["target"])
        if target == ROOT_TARGET:
            raise OracleError("the site root has no parent to break inheritance from")
        scope = _scope(state, target)
        copied = list(_parent_grants(state, target)) if p.get("copy_role_assignments") else []
        scope.inherits = False
        scope.grants = copied
    elif kind is MutationType.INHERITANCE_RESTORED:
        target = str(p["target"])
        if target == ROOT_TARGET:
            raise OracleError("the site root cannot inherit")
        scope = _scope(state, target)
        scope.inherits = True
        scope.grants = []
    elif kind is MutationType.FILE_MOVED:
        file = state.files.get(str(p["file"]))
        if file is None:
            raise OracleError(f"unknown special file {p['file']!r}")
        if str(p["to_folder"]) not in state.folders:
            raise OracleError(f"unknown destination folder {p['to_folder']!r}")
        file.folder = str(p["to_folder"])  # the file keeps its own scope (verified)
    else:  # pragma: no cover - the enum is exhaustive
        raise OracleError(f"unhandled mutation type {kind!r}")


def _applicable(mutations: Sequence[Mutation], at_time: float) -> list[Mutation]:
    chosen: list[tuple[float, int, Mutation]] = []
    for index, mutation in enumerate(mutations):
        if mutation.confirmed_at is not None and mutation.confirmed_at <= at_time:
            chosen.append((mutation.confirmed_at, index, mutation))
    chosen.sort(key=lambda item: (item[0], item[1]))
    return [m for _, _, m in chosen]


def expected_access(
    spec: TenantSpec,
    mutations: Sequence[Mutation],
    at_time: float,
    *,
    canaries: Mapping[str, Sequence[str]] | None = None,
) -> dict[str, set[str]]:
    """persona id -> canaries that persona should be able to read at `at_time`.

    `canaries` may be passed to avoid recomputing generate_canaries(spec).
    """
    all_canaries = canaries if canaries is not None else generate_canaries(spec)
    state = _build_state(spec)
    for mutation in _applicable(mutations, at_time):
        _apply(state, mutation)

    result: dict[str, set[str]] = {}
    for user in spec.users:
        if not user.persona:
            continue  # site admins and other non-personas are not evaluated
        principals = _principals(state, user.id)
        readable: set[str] = set()
        for folder in spec.folders:
            if _can_read(_folder_grants(state, folder.path), principals):
                readable.update(all_canaries.get(folder.path, ()))
        for sf in spec.special_files:
            file = state.files[sf.id]
            grants = (
                _folder_grants(state, file.folder) if file.scope.inherits else file.scope.grants
            )
            if _can_read(grants, principals):
                readable.update(all_canaries.get(special_key(sf.id), ()))
        result[user.id] = readable
    return result


class IntentTimeline:
    """expected_access over time: intent changes exactly at the confirmed_at of each mutation."""

    def __init__(self, spec: TenantSpec, mutations: Sequence[Mutation]) -> None:
        self._spec = spec
        self._canaries = generate_canaries(spec)
        self._times = sorted({m.confirmed_at for m in mutations if m.confirmed_at is not None})
        self._snapshots = [expected_access(spec, mutations, float("-inf"), canaries=self._canaries)]
        self._snapshots += [
            expected_access(spec, mutations, t, canaries=self._canaries) for t in self._times
        ]

    def expected_at(self, t: float) -> Mapping[str, set[str]]:
        return self._snapshots[bisect_right(self._times, t)]

    def change_times(self) -> Sequence[float]:
        return list(self._times)
