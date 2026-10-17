"""Real implementations of the ports on top of GraphClient and SharePointRest.

Thin on purpose: each method is one or two calls that probes already verified. The generator's
decisions live elsewhere (and are tested against fakes); these adapters are exercised by the live
test (tests/test_generator_live.py).
"""

from typing import Any, Literal, cast
from urllib.parse import quote, unquote, urlsplit

from leaklab.generator.ports import Assignment, Role
from leaklab.graph_client import GRAPH_BASE_URL, GraphClient, GraphError
from leaklab.sp_rest import SharePointRest, parse_body


class GraphGateway:
    def __init__(self, gc: GraphClient) -> None:
        self._gc = gc

    def resolve_drive(self, host: str, site_path: str) -> tuple[str, str]:
        site = self._gc.get(f"/sites/{host}:{site_path}")
        drive = self._gc.get(f"/sites/{site['id']}/drive")
        return str(drive["id"]), unquote(urlsplit(str(drive["webUrl"])).path)

    def get_user(self, upn: str) -> dict[str, Any] | None:
        return self._gc.get_user_by_upn(upn)

    def create_user(
        self, upn: str, display: str, nickname: str, password: str, usage_location: str
    ) -> dict[str, Any]:
        return self._gc.create_user(upn, display, nickname, password, usage_location)

    def user_license_sku_ids(self, user_id: str) -> set[str]:
        data = self._gc.get_user_licensing(user_id)
        return {str(lic.get("skuId")) for lic in data.get("assignedLicenses", [])}

    def list_skus(self) -> list[dict[str, Any]]:
        return self._gc.list_subscribed_skus()

    def assign_license(self, user_id: str, sku_id: str) -> None:
        self._gc.assign_license(user_id, sku_id)

    def find_group(self, name: str) -> dict[str, Any] | None:
        return self._gc.find_group_by_name(name)

    def create_group(self, name: str) -> dict[str, Any]:
        return self._gc.create_security_group(name, name)

    def add_member(self, group_id: str, member_id: str, member_is_group: bool) -> bool:
        if member_is_group:
            return self._gc.add_group_to_group(group_id, member_id)
        return self._gc.add_user_to_group(group_id, member_id)

    def remove_member(self, group_id: str, member_id: str) -> None:
        self._gc.remove_group_member(group_id, member_id)

    def user_group_ids(self, user_id: str) -> set[str]:
        return {str(g["id"]) for g in self._gc.list_transitive_member_of(user_id)}

    def delete_user(self, user_id: str) -> None:
        self._gc.delete_user(user_id)

    def delete_group(self, group_id: str) -> None:
        self._gc.delete_group(group_id)

    def find_item(self, drive_id: str, parent_id: str | None, name: str) -> dict[str, Any] | None:
        base = f"/drives/{drive_id}/items/{parent_id}" if parent_id else f"/drives/{drive_id}/root"
        try:
            return self._gc.get(f"{base}:/{quote(name)}")
        except GraphError as exc:
            if exc.status_code == 404:
                return None
            raise

    def create_folder(self, drive_id: str, parent_id: str | None, name: str) -> dict[str, Any]:
        base = f"/drives/{drive_id}/items/{parent_id}" if parent_id else f"/drives/{drive_id}/root"
        body = {"name": name, "folder": {}, "@microsoft.graph.conflictBehavior": "fail"}
        return cast(dict[str, Any], self._gc.request("POST", f"{base}/children", json=body).json())

    def upload_text(self, drive_id: str, parent_id: str, name: str, text: str) -> dict[str, Any]:
        resp = self._gc.request(
            "PUT",
            f"/drives/{drive_id}/items/{parent_id}:/{quote(name)}:/content",
            content=text,
            headers={"Content-Type": "text/plain"},
        )
        return cast(dict[str, Any], resp.json())

    def delete_item(self, drive_id: str, item_id: str) -> None:
        self._gc.request("DELETE", f"/drives/{drive_id}/items/{item_id}")

    def move_item(self, drive_id: str, item_id: str, new_parent_id: str) -> dict[str, Any]:
        resp = self._gc.request(
            "PATCH",
            f"/drives/{drive_id}/items/{item_id}",
            json={"parentReference": {"id": new_parent_id}},
        )
        return cast(dict[str, Any], resp.json())


class SharePointGateway:
    def __init__(self, sp: SharePointRest) -> None:
        self._sp = sp

    def ensure_principal(self, logon_name: str) -> dict[str, Any]:
        return cast(dict[str, Any], parse_body(self._sp.ensure_user(logon_name)))

    def role_definitions(self) -> dict[str, int]:
        data = parse_body(self._sp.get_role_definitions())
        return {str(r["Name"]): int(r["Id"]) for r in data.get("value", [])}

    def has_unique(self, item_path: str) -> bool:
        data = parse_body(self._sp.get_unique_flag(item_path))
        return bool(data["HasUniqueRoleAssignments"])

    def break_inheritance(self, item_path: str, copy_role_assignments: bool) -> None:
        self._sp.break_role_inheritance(
            item_path, copy_role_assignments=copy_role_assignments, clear_subscopes=True
        )

    def add_assignment(self, item_path: str, principal_id: int, role_id: int) -> None:
        self._sp.add_role_assignment(item_path, principal_id, role_id)

    def remove_assignment(self, item_path: str, principal_id: int, role_id: int) -> None:
        self._sp.remove_role_assignment(item_path, principal_id, role_id)

    def assignments(self, item_path: str) -> list[Assignment]:
        data = parse_body(self._sp.get_role_assignments(item_path))
        out: list[Assignment] = []
        for a in data.get("value", []):
            roles = tuple(
                Role(str(r["Name"]), int(r["Id"]), r.get("RoleTypeKind"))
                for r in a.get("RoleDefinitionBindings", [])
            )
            out.append(Assignment(str(a["Member"]["LoginName"]), int(a["PrincipalId"]), roles))
        return out

    def find_sp_group(self, title: str) -> dict[str, Any] | None:
        rows = parse_body(self._sp.find_site_group(title)).get("value", [])
        return dict(rows[0]) if rows else None

    def create_sp_group(self, title: str) -> dict[str, Any]:
        data = dict(parse_body(self._sp.create_site_group(title, "LeakLab generated")))
        # Verified: the response carries "Id". UNVERIFIED: that it carries "LoginName"; a
        # SharePoint group's login is normally its title, which verify() checks on re-read.
        data.setdefault("LoginName", title)
        return data

    def sp_group_users(self, group_id: int) -> list[str]:
        users = parse_body(self._sp.get_group_users(group_id))
        return [str(u["LoginName"]) for u in users.get("value", [])]

    def sp_group_add_user(self, group_id: int, login: str) -> None:
        self._sp.add_user_to_site_group(group_id, login)

    def sp_group_remove_user(self, group_id: int, user_id: int) -> None:
        self._sp.remove_user_from_site_group(group_id, user_id)

    def delete_sp_group(self, group_id: int) -> None:
        self._sp.delete_site_group(group_id)

    def default_group_members(self, kind: str) -> list[str]:
        group_kind = cast(Literal["owner", "member", "visitor"], kind)
        group = parse_body(self._sp.get_associated_group(group_kind))
        users = parse_body(self._sp.get_group_users(int(group["Id"])))
        return [str(u["LoginName"]) for u in users.get("value", [])]


__all__ = ["GRAPH_BASE_URL", "GraphGateway", "SharePointGateway"]
