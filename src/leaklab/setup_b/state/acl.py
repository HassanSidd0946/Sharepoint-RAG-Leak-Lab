"""acl_store: who holds which role on which scope, from Graph DRIVE DELTA + SharePoint REST.

What the probe showed (probes/delta_probe.py, 2026-10-17, docs/FINDINGS.md): with
`Prefer: deltashowremovedasdeleted, deltatraversepermissiongaps, deltashowsharingchanges` the drive
delta marks the SCOPE whose permissions changed with `@microsoft.graph.sharedChanged` (the folder or
the file with its own role assignments) within about 5-9 s: a direct grant added or removed, and
also a membership change of a SharePoint group that the scope grants to. It does NOT report an Entra
group membership change (that is the membership store's job), and without those headers it reports
nothing about permissions at all.

So every poll (5 s):
  1. read the drive delta; items flagged shared_changed, new or moved items, and deleted items are
     the only ones touched;
  2. for a flagged/new/moved item ask SharePoint REST whether it has unique permissions and, if it
     does, read its role assignments and EXPAND SharePoint groups into their users (REST);
  3. recompute which scope every item belongs to (the nearest ancestor with its own permissions,
     else the site root) and commit the new snapshot, its events and the poller state atomically.

verify_all() is the safety net the delta cannot be (the site ROOT's role assignments, and anything
the delta missed): it re-reads every scope and every item's unique flag, applies what differs and
logs each difference as "drift_found". mode "poll" (no delta; `poll_scopes` every cycle) is the
fallback the task asked for if the delta shows nothing: it re-reads the role assignments of the
unique scopes and the root only.

Fail closed: a role we cannot interpret, an Entra group inside a SharePoint group (unverified) or a
special claim becomes a Grant of kind "unknown" and NEVER grants access.
"""

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Protocol

from leaklab.setup_b.state.closure import parse_login
from leaklab.setup_b.state.directory import DirectoryStore
from leaklab.setup_b.state.model import (
    ROOT_SCOPE,
    AclSnapshot,
    DocRecord,
    DriveItem,
    DrivePage,
    Event,
    Grant,
    PollerState,
    RawAssignment,
    ResyncRequired,
)
from leaklab.setup_b.state.repository import StateRepository

NAME = "acl"
LIMITED_ACCESS = "Limited Access"  # SharePoint adds it itself; it is not read access
SP_GROUP = 8  # SharePoint PrincipalType of a SharePoint group


class DriveDeltaSource(Protocol):
    def fetch(self, link: str | None) -> DrivePage:
        """Changes since `link` (None: the whole library). Raises ResyncRequired on HTTP 410."""
        ...


class SharePointSource(Protocol):
    def has_unique(self, item: DocRecord) -> bool: ...

    def assignments(self, item: DocRecord | None) -> list[RawAssignment]:
        """Role assignments of an item; None is the site root."""
        ...

    def sp_group_users(self, group_id: int) -> list[str]:
        """Logins of the users inside a SharePoint group."""
        ...


@dataclass(frozen=True)
class AclPollResult:
    ok: bool
    events: int = 0
    error: str | None = None


class AclStore:
    def __init__(
        self,
        repo: StateRepository,
        drive: DriveDeltaSource,
        sharepoint: SharePointSource,
        clock: Callable[[], float],
        *,
        container: str | None = None,
        directory: DirectoryStore | None = None,
    ) -> None:
        self._repo = repo
        self._drive = drive
        self._sp = sharepoint
        self._clock = clock
        self._container = container  # only items under the folder with this name are tracked
        self._directory = directory  # maps the UPNs of user grants to object ids

    # -- the 5 s poll: drive delta ----------------------------------------------------------------

    def poll_once(self) -> AclPollResult:
        attempt = self._clock()
        state = self._repo.get_state(NAME)
        try:
            try:
                page = self._drive.fetch(state.link if state else None)
            except ResyncRequired:
                page = self._drive.fetch(None)
            seen_at = self._clock()
            snap = AclSnapshot() if page.full else self._repo.load_acl()
            old = self._repo.load_acl()
            refresh = self._merge_items(snap, page.items, full=page.full)
            if page.full or ROOT_SCOPE not in snap.grants:
                refresh.add(ROOT_SCOPE)
            self._refresh(snap, refresh)
            return self._commit(old, snap, seen_at, attempt, page.link, found_by="delta")
        except Exception as exc:
            return self._failed(attempt, exc)

    # -- the safety net / the polling fallback --------------------------------------------------

    def verify_all(self, *, recheck_unique: bool = True) -> AclPollResult:
        """Re-read every scope (and, by default, every item's unique flag); log what had drifted."""
        attempt = self._clock()
        try:
            old = self._repo.load_acl()
            snap = self._repo.load_acl()
            seen_at = self._clock()
            if recheck_unique:
                for item_id, item in list(snap.items.items()):
                    if self._tracked_parent(item):
                        snap.items[item_id] = replace(item, unique=self._sp.has_unique(item))
            self._refresh(snap, {ROOT_SCOPE, *(i for i, r in snap.items.items() if r.unique)})
            state = self._repo.get_state(NAME)
            return self._commit(
                old, snap, seen_at, attempt, state.link if state else None, found_by="verify"
            )
        except Exception as exc:
            return self._failed(attempt, exc)

    def poll_scopes(self) -> AclPollResult:
        """Fallback mode: re-read the role assignments of the unique scopes and the root only."""
        return self.verify_all(recheck_unique=False)

    # -- internals -------------------------------------------------------------------------------

    def _tracked_parent(self, item: DocRecord) -> bool:
        return item.parent_id is not None  # the library root has no list item to ask about

    def _merge_items(
        self, snap: AclSnapshot, items: tuple[DriveItem, ...], *, full: bool
    ) -> set[str]:
        """Apply the page to the item tree; return the scope ids whose grants must be re-read."""
        refresh: set[str] = set()
        for it in items:
            if it.deleted:
                snap.items.pop(it.item_id, None)
                snap.grants.pop(it.item_id, None)
                continue
            known = snap.items.get(it.item_id)
            record = DocRecord(
                it.item_id,
                it.name,
                it.parent_id,
                it.is_folder,
                it.web_url,
                known.unique if known else False,
            )
            changed_place = known is None or known.parent_id != it.parent_id
            # EVERY item the delta returns gets its unique flag re-read (one cheap REST call): the
            # flag alone is not trusted to announce a new scope (inheritance changes: not probed).
            if self._tracked_parent(record):
                record = replace(record, unique=self._sp.has_unique(record))
            snap.items[it.item_id] = record
            became_unique = record.unique and not (known is not None and known.unique)
            if record.unique and (
                it.shared_changed
                or changed_place
                or full
                or became_unique
                or it.item_id not in snap.grants
            ):
                refresh.add(it.item_id)
            if not record.unique:
                snap.grants.pop(it.item_id, None)
        self._drop_untracked(snap)
        return {s for s in refresh if s in snap.items}

    def _drop_untracked(self, snap: AclSnapshot) -> None:
        """Forget items outside the container and orphans whose parent chain is broken."""
        container = None
        if self._container is not None:
            container = next(
                (
                    i.doc_id
                    for i in snap.items.values()
                    if i.name == self._container and i.is_folder
                ),
                None,
            )

        def reachable(item_id: str) -> bool:
            if self._container is not None and container is None:
                return False  # the container is not (yet) in the tree: track nothing
            seen: set[str] = set()
            current: str | None = item_id
            while current is not None and current not in seen:
                seen.add(current)
                if container is not None and current == container:
                    return True
                item = snap.items.get(current)
                if item is None:
                    return False
                if item.parent_id is None:
                    return container is None
                current = item.parent_id
            return False

        for item_id in [i for i in snap.items if not reachable(i)]:
            snap.items.pop(item_id, None)
            snap.grants.pop(item_id, None)

    def _refresh(self, snap: AclSnapshot, scopes: set[str]) -> None:
        for scope in scopes:
            if scope == ROOT_SCOPE:
                snap.grants[ROOT_SCOPE] = self._grants(None)
            elif scope in snap.items and snap.items[scope].unique:
                snap.grants[scope] = self._grants(snap.items[scope])
        for scope in [s for s in snap.grants if s != ROOT_SCOPE and s not in snap.items]:
            snap.grants.pop(scope)  # a scope whose item is gone

    def _grants(self, item: DocRecord | None) -> tuple[Grant, ...]:
        """A scope's role assignments with SharePoint groups expanded into users."""
        grants: set[Grant] = set()
        for a in self._sp.assignments(item):
            roles = [r for r in a.roles if r != LIMITED_ACCESS]
            if a.principal_type == SP_GROUP:
                members = self._sp.sp_group_users(a.principal_id)
                for login in members:
                    kind, ref = parse_login(login, 1 if login.startswith("i:0#.f|") else 4)
                    for role in roles:
                        # an Entra group inside a SharePoint group is not interpreted (unverified)
                        grants.add(
                            Grant(
                                "user" if kind == "user" else "unknown",
                                ref,
                                role,
                                f"sp_group:{a.principal_id}",
                            )
                        )
                continue
            kind, ref = parse_login(a.login, a.principal_type)
            for role in roles:
                grants.add(Grant(kind, ref, role, "direct"))
        return tuple(sorted(grants, key=lambda g: (g.kind, g.ref, g.role, g.via)))

    def _commit(
        self,
        old: AclSnapshot,
        new: AclSnapshot,
        seen_at: float,
        attempt: float,
        link: str | None,
        *,
        found_by: str,
    ) -> AclPollResult:
        applied_at = self._clock()
        events = diff_events(old, new, seen_at, applied_at, found_by)
        with self._repo.atomic():
            if old != new:
                self._repo.save_acl(new)
            if self._directory is not None:
                self._directory.refresh(self._repo)
            self._repo.append_events(events)
            self._repo.set_state(PollerState(NAME, link, applied_at, attempt, None))
        return AclPollResult(ok=True, events=len(events))

    def _failed(self, attempt: float, exc: Exception) -> AclPollResult:
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
        return AclPollResult(ok=False, error=error)


def diff_events(
    old: AclSnapshot, new: AclSnapshot, seen_at: float, applied_at: float, found_by: str
) -> list[Event]:
    """One event per item added/removed/moved, per scope that became (or stopped being) unique and
    per grant added/removed. `found_by` says which mechanism noticed ("delta" or "verify"; a change
    first found by "verify" is a change the delta missed)."""

    def event(kind: str, subject: str, detail: dict[str, object]) -> Event:
        return Event("acl", kind, subject, {**detail, "found_by": found_by}, seen_at, applied_at)

    out: list[Event] = []
    for item_id, item in sorted(new.items.items()):
        before = old.items.get(item_id)
        if before is None:
            out.append(event("item_added", item_id, {"name": item.name}))
        else:
            if before.parent_id != item.parent_id:
                out.append(event("item_moved", item_id, {"name": item.name, "to": item.parent_id}))
            if before.unique != item.unique:
                kind = "scope_became_unique" if item.unique else "scope_inherits_again"
                out.append(event(kind, item_id, {"name": item.name}))
    for item_id, item in sorted(old.items.items()):
        if item_id not in new.items:
            out.append(event("item_removed", item_id, {"name": item.name}))
    for scope in sorted(set(old.grants) | set(new.grants)):
        before_g, after_g = set(old.grants.get(scope, ())), set(new.grants.get(scope, ()))
        name = (
            new.items.get(scope)
            or old.items.get(scope)
            or DocRecord(scope, scope, None, False, None, False)
        ).name
        for g in sorted(after_g - before_g, key=_grant_key):
            out.append(event("grant_added", scope, _grant_detail(name, g)))
        for g in sorted(before_g - after_g, key=_grant_key):
            out.append(event("grant_removed", scope, _grant_detail(name, g)))
    return out


def _grant_key(g: Grant) -> tuple[str, str, str, str]:
    return (g.kind, g.ref, g.role, g.via)


def _grant_detail(scope_name: str, g: Grant) -> dict[str, object]:
    return {"scope_name": scope_name, "kind": g.kind, "ref": g.ref, "role": g.role, "via": g.via}
