"""An in-memory fake tenant implementing the generator's GraphPort and SharePointPort.

It behaves like what docs/FINDINGS.md records: a new group answers 404 to the first member adds,
ensureuser can answer HTTP 400 "could not be found" a few times, adding an existing member is
reported as "already", deleting something gone is a 404, breaking inheritance with copy=false
leaves an item with no grants, SharePoint adds "Limited Access" entries on its own, and a moved
item keeps its permissions. Failure injection and call counters let tests check the retries.
"""

import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote

from leaklab.generator.ports import Assignment, Role
from leaklab.graph_client import GraphError
from leaklab.sp_rest import SPRestError

LIBRARY = "/sites/T/Shared Documents"
ROLE_IDS = {
    "Full Control": 1073741829,
    "Edit": 1073741830,
    "Read": 1073741826,
    "Limited Access": 1073741825,
}
ROLE_KINDS = {"Full Control": 5, "Edit": 6, "Read": 2, "Limited Access": 1}


class FakeClock:
    """A clock that only moves when somebody sleeps."""

    def __init__(self) -> None:
        self.now = 1_000_000.0
        self.slept: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


@dataclass
class Item:
    id: str
    name: str
    parent: str | None
    is_folder: bool
    unique: bool = False
    grants: dict[str, set[str]] = field(default_factory=dict)  # login -> role names


class FakeTenant:
    def __init__(self) -> None:
        self._n = 0
        self.users: dict[str, dict[str, Any]] = {}  # upn lower -> user
        self.groups: dict[str, dict[str, Any]] = {}  # name -> group
        self.items: dict[str, Item] = {}
        self.principals: dict[str, int] = {}  # login -> sharepoint id
        self.web_grants: dict[str, set[str]] = {
            "LeakLab Owners": {"Full Control"},
            "LeakLab Members": {"Edit"},
            "LeakLab Visitors": {"Read"},
        }
        # failure injection / counters
        self.add_member_404s = 0  # the next N member adds fail with 404 (a new group)
        self.ensure_failures = 0  # the next N ensureuser calls fail with 400 "could not be found"
        self.ensure_failures_raised = 0
        self.add_member_404s_raised = 0
        self.ignore_removals = False  # remove_assignment silently does nothing
        self.calls: dict[str, int] = {}
        self.graph = FakeGraph(self)
        self.sp = FakeSharePoint(self)

    def new_id(self, prefix: str) -> str:
        self._n += 1
        return f"{prefix}-{self._n}"

    def count(self, name: str) -> None:
        self.calls[name] = self.calls.get(name, 0) + 1

    def snapshot(self) -> dict[str, Any]:
        """Everything that matters, for 'a re-run changes nothing' comparisons."""
        return {
            "users": {u: sorted(v["licenses"]) for u, v in self.users.items()},
            "groups": {n: sorted(map(str, g["members"])) for n, g in self.groups.items()},
            "items": {
                i.id: (i.name, i.parent, i.unique, {k: sorted(v) for k, v in i.grants.items()})
                for i in self.items.values()
            },
            "web": {k: sorted(v) for k, v in self.web_grants.items()},
        }


def _path_segments(item_path: str) -> list[str] | None:
    """The folders/files below the library in a REST item path; None for "/web"."""
    if item_path == "/web":
        return None
    match = re.search(r"\('([^']*)'\)", item_path)
    assert match, item_path
    url = unquote(match.group(1))
    assert url.startswith(LIBRARY), url
    return [s for s in url[len(LIBRARY) :].split("/") if s]


class FakeGraph:
    def __init__(self, tenant: FakeTenant) -> None:
        self.t = tenant

    def resolve_drive(self, host: str, site_path: str) -> tuple[str, str]:
        return "drive1", LIBRARY

    def get_user(self, upn: str) -> dict[str, Any] | None:
        return self.t.users.get(upn.lower())

    def create_user(
        self, upn: str, display: str, nickname: str, password: str, usage_location: str
    ) -> dict[str, Any]:
        assert password and usage_location
        user = {"id": self.t.new_id("user"), "userPrincipalName": upn, "licenses": set()}
        self.t.users[upn.lower()] = user
        return user

    def user_license_sku_ids(self, user_id: str) -> set[str]:
        return set(next(u for u in self.t.users.values() if u["id"] == user_id)["licenses"])

    def list_skus(self) -> list[dict[str, Any]]:
        return [
            {
                "skuId": "sku-1",
                "skuPartNumber": "O365_BUSINESS_ESSENTIALS",
                "appliesTo": "User",
                "capabilityStatus": "Enabled",
                "prepaidUnits": {"enabled": 25},
                "consumedUnits": 1,
            }
        ]

    def assign_license(self, user_id: str, sku_id: str) -> None:
        next(u for u in self.t.users.values() if u["id"] == user_id)["licenses"].add(sku_id)

    def find_group(self, name: str) -> dict[str, Any] | None:
        return self.t.groups.get(name)

    def create_group(self, name: str) -> dict[str, Any]:
        group = {"id": self.t.new_id("group"), "displayName": name, "members": set(), "new": True}
        self.t.groups[name] = group
        return group

    def _group_by_id(self, group_id: str) -> dict[str, Any]:
        found = next((g for g in self.t.groups.values() if g["id"] == group_id), None)
        if found is None:
            raise GraphError(404, "Request_ResourceNotFound", "group not found")
        return found

    def add_member(self, group_id: str, member_id: str, member_is_group: bool) -> bool:
        group = self._group_by_id(group_id)
        if self.t.add_member_404s > 0:
            self.t.add_member_404s -= 1
            self.t.add_member_404s_raised += 1
            raise GraphError(404, "Request_ResourceNotFound", "not replicated yet")
        key = ("group" if member_is_group else "user", member_id)
        if key in group["members"]:
            return False  # Graph: 400 "already exist", reported as False by GraphClient
        group["members"].add(key)
        return True

    def remove_member(self, group_id: str, member_id: str) -> None:
        group = self._group_by_id(group_id)
        group["members"] = {m for m in group["members"] if m[1] != member_id}

    def user_group_ids(self, user_id: str) -> set[str]:
        found: set[str] = set()
        frontier = [("user", user_id)]
        while frontier:
            kind_id = frontier.pop()
            for group in self.t.groups.values():
                if kind_id in group["members"] and group["id"] not in found:
                    found.add(group["id"])
                    frontier.append(("group", group["id"]))
        return found

    def delete_user(self, user_id: str) -> None:
        key = next((k for k, u in self.t.users.items() if u["id"] == user_id), None)
        if key is None:
            raise GraphError(404, "Request_ResourceNotFound", "user not found")
        del self.t.users[key]

    def delete_group(self, group_id: str) -> None:
        name = next((n for n, g in self.t.groups.items() if g["id"] == group_id), None)
        if name is None:
            raise GraphError(404, "Request_ResourceNotFound", "group not found")
        del self.t.groups[name]

    def find_item(self, drive_id: str, parent_id: str | None, name: str) -> dict[str, Any] | None:
        for item in self.t.items.values():
            if item.parent == parent_id and item.name == name:
                return {"id": item.id, "name": item.name}
        return None

    def create_folder(self, drive_id: str, parent_id: str | None, name: str) -> dict[str, Any]:
        assert self.find_item(drive_id, parent_id, name) is None, "conflictBehavior=fail"
        item = Item(self.t.new_id("item"), name, parent_id, True)
        self.t.items[item.id] = item
        return {"id": item.id, "name": name}

    def upload_text(self, drive_id: str, parent_id: str, name: str, text: str) -> dict[str, Any]:
        existing = self.find_item(drive_id, parent_id, name)
        if existing:
            return existing
        item = Item(self.t.new_id("item"), name, parent_id, False)
        self.t.items[item.id] = item
        return {"id": item.id, "name": name}

    def delete_item(self, drive_id: str, item_id: str) -> None:
        if item_id not in self.t.items:
            raise GraphError(404, "itemNotFound", "gone")
        doomed = {item_id}
        changed = True
        while changed:
            changed = False
            for i in self.t.items.values():
                if i.parent in doomed and i.id not in doomed:
                    doomed.add(i.id)
                    changed = True
        for i in doomed:
            del self.t.items[i]

    def move_item(self, drive_id: str, item_id: str, new_parent_id: str) -> dict[str, Any]:
        self.t.items[item_id].parent = new_parent_id  # the item keeps its own permissions
        return {"id": item_id, "parentReference": {"id": new_parent_id}}


class FakeSharePoint:
    def __init__(self, tenant: FakeTenant) -> None:
        self.t = tenant
        self.default_members: dict[str, list[str]] = {
            "owner": ["admin"],
            "member": [],
            "visitor": [],
        }

    def _item(self, item_path: str) -> Item | None:
        segments = _path_segments(item_path)
        if segments is None:
            return None
        parent: str | None = None
        item: Item | None = None
        for name in segments:
            item = next(
                (i for i in self.t.items.values() if i.parent == parent and i.name == name), None
            )
            if item is None:
                raise SPRestError(404, item_path, {"error": "item not found"})
            parent = item.id
        assert item is not None
        return item

    def ensure_principal(self, logon_name: str) -> dict[str, Any]:
        self.t.count("ensure_principal")
        if self.t.ensure_failures > 0:
            self.t.ensure_failures -= 1
            self.t.ensure_failures_raised += 1
            raise SPRestError(
                400,
                "/web/ensureuser",
                {
                    "odata.error": {
                        "message": {"value": f"The specified user {logon_name} could not be found."}
                    }
                },
            )
        if logon_name.startswith("c:0t.c|tenant|"):
            gid = logon_name.rsplit("|", 1)[1]
            if not any(g["id"] == gid for g in self.t.groups.values()):
                raise SPRestError(400, "/web/ensureuser", {"error": "could not be found"})
            login, kind = logon_name, 4
        else:
            if logon_name.lower() not in self.t.users:
                raise SPRestError(400, "/web/ensureuser", {"error": "could not be found"})
            login, kind = f"i:0#.f|membership|{logon_name.lower()}", 1
        pid = self.t.principals.setdefault(login, len(self.t.principals) + 10)
        return {"Id": pid, "LoginName": login, "PrincipalType": kind}

    def role_definitions(self) -> dict[str, int]:
        return dict(ROLE_IDS)

    def has_unique(self, item_path: str) -> bool:
        item = self._item(item_path)
        return True if item is None else item.unique

    def _grants_for(self, item: Item | None) -> dict[str, set[str]]:
        """Own grants if unique, else the nearest unique ancestor's, else the site root's."""
        current = item
        while current is not None:
            if current.unique:
                return current.grants
            current = self.t.items.get(current.parent) if current.parent else None
        return self.t.web_grants

    def break_inheritance(self, item_path: str, copy_role_assignments: bool) -> None:
        item = self._item(item_path)
        assert item is not None
        inherited = {k: set(v) for k, v in self._grants_for(item).items()}
        item.unique = True
        item.grants = inherited if copy_role_assignments else {}

    def _login_of(self, principal_id: int) -> str:
        return next(login for login, pid in self.t.principals.items() if pid == principal_id)

    def add_assignment(self, item_path: str, principal_id: int, role_id: int) -> None:
        role = next(n for n, i in ROLE_IDS.items() if i == role_id)
        login = self._login_of(principal_id)
        item = self._item(item_path)
        if item is None:
            self.t.web_grants.setdefault(login, set()).add(role)
        else:
            assert item.unique, "grants need unique permissions"
            item.grants.setdefault(login, set()).add(role)
            # SharePoint adds "Limited Access" for the principal on every unique ancestor and on the
            # site root by itself (seen with nested folders and root grants).
            parent = self.t.items.get(item.parent) if item.parent else None
            while parent is not None:
                if parent.unique:
                    parent.grants.setdefault(login, set()).add("Limited Access")
                parent = self.t.items.get(parent.parent) if parent.parent else None
            self.t.web_grants.setdefault(login, set()).add("Limited Access")

    def remove_assignment(self, item_path: str, principal_id: int, role_id: int) -> None:
        if self.t.ignore_removals:
            return
        role = next(n for n, i in ROLE_IDS.items() if i == role_id)
        login = self._login_of(principal_id)
        item = self._item(item_path)
        grants = self.t.web_grants if item is None else item.grants
        grants.get(login, set()).discard(role)
        if not grants.get(login):
            grants.pop(login, None)

    def assignments(self, item_path: str) -> list[Assignment]:
        grants = self._grants_for(self._item(item_path))
        out = []
        for login, roles in grants.items():
            pid = self.t.principals.get(login, 100 + sum(map(ord, login)) % 900)
            out.append(
                Assignment(
                    login, pid, tuple(Role(r, ROLE_IDS[r], ROLE_KINDS[r]) for r in sorted(roles))
                )
            )
        return out

    def default_group_members(self, kind: str) -> list[str]:
        return list(self.default_members[kind])
