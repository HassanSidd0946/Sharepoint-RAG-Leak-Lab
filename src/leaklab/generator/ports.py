"""The narrow Graph and SharePoint operations the generator uses, as Protocols.

The generator talks to these, never to HTTP directly, so its logic can be tested against an
in-memory fake tenant (tests/generator_fakes.py) and run for real through gateways.py. Every
operation here is one that docs/FINDINGS.md records as verified; operations that are not verified
(listing a folder's children, listing a group's direct members, resetting inheritance, deleting
SharePoint site users) are deliberately absent.

Errors: the real gateways raise GraphError / SPRestError; fakes raise the same types.
"""

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class Role:
    name: str
    id: int
    kind: int | None  # RoleTypeKind (1 = Limited Access)


@dataclass(frozen=True)
class Assignment:
    login: str
    principal_id: int
    roles: tuple[Role, ...]


class GraphPort(Protocol):
    def resolve_drive(self, host: str, site_path: str) -> tuple[str, str]:
        """(drive id, library server-relative URL such as /sites/X/Shared Documents)."""
        ...

    def get_user(self, upn: str) -> dict[str, Any] | None: ...

    def create_user(
        self, upn: str, display: str, nickname: str, password: str, usage_location: str
    ) -> dict[str, Any]: ...

    def user_license_sku_ids(self, user_id: str) -> set[str]: ...

    def list_skus(self) -> list[dict[str, Any]]: ...

    def assign_license(self, user_id: str, sku_id: str) -> None: ...

    def find_group(self, name: str) -> dict[str, Any] | None: ...

    def create_group(self, name: str) -> dict[str, Any]: ...

    def add_member(self, group_id: str, member_id: str, member_is_group: bool) -> bool:
        """True if added, False if it already was a member (HTTP 400 "already exist")."""
        ...

    def remove_member(self, group_id: str, member_id: str) -> None: ...

    def user_group_ids(self, user_id: str) -> set[str]:
        """Ids of all groups the user is in, at any depth (transitiveMemberOf)."""
        ...

    def delete_user(self, user_id: str) -> None: ...

    def delete_group(self, group_id: str) -> None: ...

    def find_item(self, drive_id: str, parent_id: str | None, name: str) -> dict[str, Any] | None:
        """A folder or file by name under `parent_id` (None: the library root)."""
        ...

    def create_folder(self, drive_id: str, parent_id: str | None, name: str) -> dict[str, Any]: ...

    def upload_text(
        self, drive_id: str, parent_id: str, name: str, text: str
    ) -> dict[str, Any]: ...

    def delete_item(self, drive_id: str, item_id: str) -> None: ...

    def move_item(self, drive_id: str, item_id: str, new_parent_id: str) -> dict[str, Any]: ...


class SharePointPort(Protocol):
    def ensure_principal(self, logon_name: str) -> dict[str, Any]:
        """Resolve a user (plain UPN) or an Entra group claim to a SharePoint principal."""
        ...

    def role_definitions(self) -> dict[str, int]:
        """Role name -> role definition id (looked up, never hardcoded)."""
        ...

    def has_unique(self, item_path: str) -> bool: ...

    def break_inheritance(self, item_path: str, copy_role_assignments: bool) -> None: ...

    def add_assignment(self, item_path: str, principal_id: int, role_id: int) -> None: ...

    def remove_assignment(self, item_path: str, principal_id: int, role_id: int) -> None: ...

    def assignments(self, item_path: str) -> list[Assignment]: ...

    def default_group_members(self, kind: str) -> list[str]:
        """Logins in the site's Owners/Members/Visitors group ("owner"|"member"|"visitor")."""
        ...
