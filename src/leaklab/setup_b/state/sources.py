"""The real sources behind the pollers: Graph group delta, Graph drive delta, SharePoint REST.

Only behaviors recorded as verified in docs/FINDINGS.md are used:
  - GET /groups/delta?$select=displayName,members: the first call returns every group with its FULL
    membership in `members@delta`; later calls (the deltaLink, with `Prefer: return=minimal`)
    return only the groups that changed, each member as {"@odata.type": user|group, "id"} and,
    for a removal, with "@removed": {"reason": "deleted"}. Only the changed group appears: when
    a user leaves a child group the PARENT is not returned (nesting is resolved locally).
    (probes/group_delta_probe.py, 2026-10-17; the change shows up 11-24 s after the call.)
  - GET /drives/{id}/root/delta with `Prefer: deltashowremovedasdeleted,
    deltatraversepermissiongaps, deltashowsharingchanges`: the scope whose permissions changed
    carries `@microsoft.graph.sharedChanged: true` within about 5-9 s (probes/delta_probe.py).
    The Prefer header must accompany EVERY call, the links included.
  - SharePoint REST: HasUniqueRoleAssignments, roleassignments with Member and bindings, and
    the users of a SharePoint group (all verified earlier).
A 410 from Graph becomes ResyncRequired. Nothing here retries beyond what GraphClient and
SharePointRest already do.
"""

from collections.abc import Sequence
from typing import Any
from urllib.parse import unquote, urlsplit

from leaklab.graph_client import GraphClient, GraphError
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
from leaklab.sp_rest import SharePointRest, file_item_path, folder_item_path, parse_body

DRIVE_PREFER = "deltashowremovedasdeleted, deltatraversepermissiongaps, deltashowsharingchanges"
MAX_PAGES = 200


def _pages(
    gc: GraphClient, url: str, params: dict[str, str] | None, headers: dict[str, str]
) -> tuple[list[dict[str, Any]], str]:
    """Follow @odata.nextLink to the end; return every item and the final @odata.deltaLink."""
    items: list[dict[str, Any]] = []
    next_url: str | None = url
    next_params = params
    for _ in range(MAX_PAGES):
        assert next_url is not None
        try:
            page = gc.request("GET", next_url, params=next_params, headers=headers).json()
        except GraphError as exc:
            if exc.status_code == 410:
                raise ResyncRequired(str(exc)) from exc
            raise
        items += page.get("value", [])
        next_params = None
        if "@odata.nextLink" in page:
            next_url = page["@odata.nextLink"]
            continue
        return items, str(page["@odata.deltaLink"])
    raise RuntimeError("delta paging did not end")


class GraphGroupDelta:
    def __init__(self, gc: GraphClient) -> None:
        self._gc = gc

    def fetch(self, link: str | None) -> GroupPage:
        if link is None:
            raw, new_link = _pages(
                self._gc, "/groups/delta", {"$select": "displayName,members"}, {}
            )
            return GroupPage(tuple(_group(g) for g in raw), new_link, full=True)
        raw, new_link = _pages(self._gc, link, None, {"Prefer": "return=minimal"})
        return GroupPage(tuple(_group(g) for g in raw), new_link, full=False)


def _group(obj: dict[str, Any]) -> MembershipChange:
    added: list[tuple[MemberKind, str]] = []
    removed: list[tuple[MemberKind, str]] = []
    for m in obj.get("members@delta", []) or []:
        kind: MemberKind
        if m.get("@odata.type") == "#microsoft.graph.user":
            kind = "user"
        elif m.get("@odata.type") == "#microsoft.graph.group":
            kind = "group"
        else:
            continue  # service principals, devices, contacts: not a way for a person to read
        (removed if "@removed" in m else added).append((kind, str(m["id"]).lower()))
    return MembershipChange(
        group_id=str(obj["id"]).lower(),
        display_name=obj.get("displayName"),
        deleted="@removed" in obj,
        added=tuple(added),
        removed=tuple(removed),
    )


class GraphDirectory:
    def __init__(self, gc: GraphClient) -> None:
        self._gc = gc

    def resolve(self, user_ids: Sequence[str]) -> dict[str, str]:
        out: dict[str, str] = {}
        for user_id in user_ids:
            try:
                data = self._gc.get(f"/users/{user_id}", {"$select": "userPrincipalName"})
            except GraphError as exc:
                if exc.status_code == 404:
                    continue  # not a user (or deleted): it simply has no UPN
                raise
            upn = data.get("userPrincipalName")
            if upn:
                out[user_id.lower()] = str(upn).lower()
        return out

    def resolve_upns(self, upns: Sequence[str]) -> dict[str, str]:
        out: dict[str, str] = {}
        for upn in upns:
            try:
                data = self._gc.get(f"/users/{upn}", {"$select": "id,userPrincipalName"})
            except GraphError as exc:
                if exc.status_code == 404:
                    continue
                raise
            if data.get("id"):
                out[str(data["id"]).lower()] = str(data.get("userPrincipalName") or upn).lower()
        return out


class GraphDriveDelta:
    def __init__(self, gc: GraphClient, drive_id: str) -> None:
        self._gc = gc
        self._drive_id = drive_id

    def fetch(self, link: str | None) -> DrivePage:
        headers = {"Prefer": DRIVE_PREFER}
        url = link or f"/drives/{self._drive_id}/root/delta"
        raw, new_link = _pages(self._gc, url, None, headers)
        return DrivePage(tuple(_drive_item(o) for o in raw), new_link, full=link is None)


def _drive_item(obj: dict[str, Any]) -> DriveItem:
    is_root = "root" in obj
    return DriveItem(
        item_id=str(obj["id"]),
        name=str(obj.get("name") or ""),
        parent_id=None if is_root else (obj.get("parentReference") or {}).get("id"),
        is_folder="folder" in obj or is_root,
        deleted="deleted" in obj,
        shared_changed=obj.get("@microsoft.graph.sharedChanged") in (True, "True", "true"),
        web_url=obj.get("webUrl"),
    )


class RestSharePoint:
    def __init__(self, sp: SharePointRest) -> None:
        self._sp = sp

    @staticmethod
    def _path(item: DocRecord) -> str:
        if not item.web_url:
            raise ValueError(f"item {item.doc_id} has no webUrl: cannot address it through REST")
        server_relative = unquote(urlsplit(item.web_url).path)
        return (
            file_item_path(server_relative)
            if not item.is_folder
            else folder_item_path(server_relative)
        )

    def has_unique(self, item: DocRecord) -> bool:
        return bool(
            parse_body(self._sp.get_unique_flag(self._path(item)))["HasUniqueRoleAssignments"]
        )

    def assignments(self, item: DocRecord | None) -> list[RawAssignment]:
        path = "/web" if item is None else self._path(item)
        data = parse_body(self._sp.get_role_assignments(path))
        out: list[RawAssignment] = []
        for a in data.get("value", []):
            member = a.get("Member", {})
            out.append(
                RawAssignment(
                    login=str(member.get("LoginName", "")),
                    principal_id=int(a["PrincipalId"]),
                    principal_type=int(member.get("PrincipalType", 0)),
                    roles=tuple(str(r["Name"]) for r in a.get("RoleDefinitionBindings", [])),
                )
            )
        return out

    def sp_group_users(self, group_id: int) -> list[str]:
        data = parse_body(self._sp.get_group_users(group_id))
        return [str(u["LoginName"]) for u in data.get("value", [])]
