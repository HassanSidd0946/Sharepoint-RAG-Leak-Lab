"""Fake sources for the Setup B state stores, built on the in-memory fake tenant.

They reproduce what the real probes showed (docs/FINDINGS.md), nothing more:
  FakeGroupDelta    first call: every group with its full membership; later calls: only the groups
                    whose direct membership changed, the changed group alone (not its parents).
  FakeDriveDelta    items of the library; after the first call only items that changed. An item whose
                    OWN permissions changed, or whose permissions depend on a SharePoint group whose
                    membership changed, is flagged shared_changed. A change of an ENTRA group's
                    membership is NOT flagged (the real delta does not report it either).
  FakeSharePoint    has_unique / role assignments / SharePoint group users through the tenant's
                    own REST fake, the same path building as the real adapter.
Failure injection: `fail_next` makes the next fetch raise; `resync_next` raises ResyncRequired once.
"""

from collections.abc import Sequence
from typing import Any
from urllib.parse import quote, unquote, urlsplit

from generator_fakes import LIBRARY, FakeTenant
from leaklab.setup_b.state.closure import GROUP_PREFIX, USER_PREFIX
from leaklab.setup_b.state.model import (
    DocRecord,
    DriveItem,
    DrivePage,
    GroupPage,
    MemberKind,
    MembershipChange,
    RawAssignment,
    ResyncRequired,
)
from leaklab.sp_rest import file_item_path, folder_item_path

LIB_ROOT = "lib-root"
HOST = "https://t.sharepoint.com"


class FakeGroupDelta:
    def __init__(self, tenant: FakeTenant) -> None:
        self.t = tenant
        self._last: dict[str, set[tuple[str, str]]] | None = None
        self.fail_next: Exception | None = None
        self.resync_next = False
        self.fetches: list[str | None] = []

    def _now(self) -> dict[str, set[tuple[str, str]]]:
        return {g["id"]: set(g["members"]) for g in self.t.groups.values()}

    def fetch(self, link: str | None) -> GroupPage:
        self.fetches.append(link)
        if self.fail_next is not None:
            error, self.fail_next = self.fail_next, None
            raise error
        if self.resync_next:
            self.resync_next = False
            raise ResyncRequired("410 gone")
        now = self._now()
        if link is None or self._last is None:
            self._last = now
            changes = tuple(
                MembershipChange(gid, added=tuple((_kind(k), m) for k, m in sorted(members)))
                for gid, members in sorted(now.items())
            )
            return GroupPage(changes, "token", full=True)
        changes_list: list[MembershipChange] = []
        for gid in sorted(set(now) | set(self._last)):
            before, after = self._last.get(gid, set()), now.get(gid)
            if after is None:
                changes_list.append(MembershipChange(gid, deleted=True))
            elif after != before:
                changes_list.append(
                    MembershipChange(
                        gid,
                        added=tuple((_kind(k), m) for k, m in sorted(after - before)),
                        removed=tuple((_kind(k), m) for k, m in sorted(before - after)),
                    )
                )
        self._last = now
        return GroupPage(tuple(changes_list), "token", full=False)


def _kind(value: str) -> MemberKind:
    return "group" if value == "group" else "user"


class FakeDirectory:
    def __init__(self, tenant: FakeTenant) -> None:
        self.t = tenant
        self.fail = False

    def resolve(self, user_ids: Sequence[str]) -> dict[str, str]:
        if self.fail:
            raise RuntimeError("directory down")
        by_id = {u["id"]: upn for upn, u in self.t.users.items()}
        return {i: by_id[i] for i in user_ids if i in by_id}

    def resolve_upns(self, upns: Sequence[str]) -> dict[str, str]:
        if self.fail:
            raise RuntimeError("directory down")
        by_upn = {upn.lower(): u["id"] for upn, u in self.t.users.items()}
        return {by_upn[u.lower()]: u.lower() for u in upns if u.lower() in by_upn}


def item_web_url(tenant: FakeTenant, item_id: str) -> str:
    names: list[str] = []
    current: str | None = item_id
    while current is not None:
        item = tenant.items[current]
        names.append(item.name)
        current = item.parent
    return HOST + quote(LIBRARY + "/" + "/".join(reversed(names)))


class FakeDriveDelta:
    def __init__(self, tenant: FakeTenant, *, report_shared_changes: bool = True) -> None:
        self.t = tenant
        self.report = report_shared_changes  # False: a delta that misses permission changes
        self._last: dict[str, tuple[Any, Any]] | None = None
        self.fail_next: Exception | None = None
        self.fetches: list[str | None] = []

    def _fingerprints(self) -> dict[str, tuple[Any, Any]]:
        """item id -> (structure fingerprint, permission fingerprint)."""
        out: dict[str, tuple[Any, Any]] = {}
        for item in self.t.items.values():
            grants = {k: frozenset(v) for k, v in item.grants.items() if item.unique}
            sp_members = {
                login: frozenset(g["members"])
                for login in grants
                for g in self.t.sp_groups.values()
                if g["LoginName"] == login
            }
            out[item.id] = (
                (item.name, item.parent, item.unique),
                (
                    tuple(sorted(grants.items(), key=str)),
                    tuple(sorted(sp_members.items(), key=str)),
                ),
            )
        return out

    def _drive_item(self, item_id: str, *, shared_changed: bool = False) -> DriveItem:
        item = self.t.items[item_id]
        return DriveItem(
            item_id,
            item.name,
            item.parent or LIB_ROOT,
            item.is_folder,
            shared_changed=shared_changed,
            web_url=item_web_url(self.t, item_id),
        )

    def fetch(self, link: str | None) -> DrivePage:
        self.fetches.append(link)
        if self.fail_next is not None:
            error, self.fail_next = self.fail_next, None
            raise error
        now = self._fingerprints()
        root = DriveItem(LIB_ROOT, "root", None, True, web_url=HOST + quote(LIBRARY))
        if link is None or self._last is None:
            self._last = now
            return DrivePage(
                (root, *(self._drive_item(i) for i in sorted(now))), "token", full=True
            )
        items: list[DriveItem] = []
        for item_id in sorted(set(now) | set(self._last)):
            if item_id not in now:
                items.append(DriveItem(item_id, "", None, False, deleted=True))
                continue
            before = self._last.get(item_id)
            if before is None or before != now[item_id]:
                permission_changed = before is None or before[1] != now[item_id][1]
                items.append(
                    self._drive_item(item_id, shared_changed=self.report and permission_changed)
                )
        self._last = now
        return DrivePage(tuple(items), "token", full=False)


class FakeSharePointSource:
    """RestSharePoint's logic over the tenant's own REST fake."""

    def __init__(self, tenant: FakeTenant) -> None:
        self.t = tenant
        self.calls: list[str] = []

    def _path(self, item: DocRecord) -> str:
        assert item.web_url
        server_relative = unquote(urlsplit(item.web_url).path)
        return (
            folder_item_path(server_relative) if item.is_folder else file_item_path(server_relative)
        )

    def has_unique(self, item: DocRecord) -> bool:
        self.calls.append(f"has_unique {item.name}")
        return self.t.sp.has_unique(self._path(item))

    def assignments(self, item: DocRecord | None) -> list[RawAssignment]:
        self.calls.append(f"assignments {item.name if item else 'root'}")
        path = "/web" if item is None else self._path(item)
        sp_titles = {g["LoginName"] for g in self.t.sp_groups.values()}
        out = []
        for a in self.t.sp.assignments(path):
            if a.login.startswith(USER_PREFIX):
                ptype = 1
            elif a.login.startswith(GROUP_PREFIX):
                ptype = 4
            elif a.login in sp_titles:
                ptype = 8
            else:
                ptype = 0
            out.append(
                RawAssignment(a.login, a.principal_id, ptype, tuple(r.name for r in a.roles))
            )
        return out

    def sp_group_users(self, group_id: int) -> list[str]:
        self.calls.append(f"sp_group_users {group_id}")
        return self.t.sp.sp_group_users(group_id)
