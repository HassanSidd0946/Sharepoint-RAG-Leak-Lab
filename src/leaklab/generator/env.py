"""Shared context for the generator: ports, caches, name lookups and retrying helpers.

An Env holds the spec, its plan, the Graph/SharePoint ports, the mutation log, an injectable
clock/sleep, and caches of the ids it has looked up. Every lookup falls back to asking the tenant,
so a fresh process (verify, teardown, apply_mutation) works without any state from an earlier run.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from leaklab.generator.log import MutationLog
from leaklab.generator.plan import GeneratorError, GrantPlan, Plan, build_plan
from leaklab.generator.ports import GraphPort, SharePointPort
from leaklab.generator.retry import (
    ENSUREUSER_TIMEOUT_S,
    MEMBER_ADD_TIMEOUT_S,
    RETRY_INTERVAL_S,
    is_ensureuser_not_found,
    is_member_add_not_ready,
    retry_call,
)
from leaklab.sp_rest import file_item_path, folder_item_path
from leaklab.spec import TenantSpec

LIMITED_ACCESS = "Limited Access"


def user_login(upn: str) -> str:
    """The SharePoint login of a user (verified: ensureuser returns it for a plain UPN)."""
    return f"i:0#.f|membership|{upn.lower()}"


def group_claim(group_object_id: str) -> str:
    """The SharePoint claim of an Entra security group (verified)."""
    return f"c:0t.c|tenant|{group_object_id}"


@dataclass
class GenSettings:
    password: str
    usage_location: str
    site_host: str
    site_path: str
    sku_part_number: str = ""
    ensureuser_timeout_s: float = ENSUREUSER_TIMEOUT_S
    member_add_timeout_s: float = MEMBER_ADD_TIMEOUT_S
    retry_interval_s: float = RETRY_INTERVAL_S
    confirm_timeout_s: float = 120.0
    confirm_interval_s: float = 2.0
    membership_settle_s: float = 90.0


@dataclass
class Principal:
    login: str
    sp_id: int


@dataclass
class Env:
    spec: TenantSpec
    graph: GraphPort
    sp: SharePointPort
    settings: GenSettings
    log: MutationLog
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.time
    plan: Plan = field(init=False)
    events: list[dict[str, Any]] = field(default_factory=list)
    _drive: tuple[str, str] | None = None
    _container_id: str | None = None
    _user_ids: dict[str, str] = field(default_factory=dict)
    _group_ids: dict[str, str] = field(default_factory=dict)
    _folder_ids: dict[str, str] = field(default_factory=dict)
    _file_ids: dict[str, str] = field(default_factory=dict)
    _principals: dict[tuple[str, str], Principal] = field(default_factory=dict)
    _roles: dict[str, int] | None = None
    special_folder: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.plan = build_plan(self.spec)
        self.special_folder = {s.id: s.folder for s in self.plan.special_files}

    # -- bookkeeping -------------------------------------------------------------------------

    def event(self, kind: str, **info: Any) -> None:
        self.events.append({"t": round(self.clock(), 3), "event": kind, **info})

    def retry(
        self, op: Callable[[], Any], retryable: Callable[[Exception], bool], timeout_s: float
    ) -> Any:
        return retry_call(
            op,
            retryable=retryable,
            timeout_s=timeout_s,
            interval_s=self.settings.retry_interval_s,
            sleep=self.sleep,
            clock=self.clock,
        )

    # -- the library ---------------------------------------------------------------------------

    @property
    def drive(self) -> tuple[str, str]:
        if self._drive is None:
            self._drive = self.graph.resolve_drive(self.settings.site_host, self.settings.site_path)
        return self._drive

    @property
    def drive_id(self) -> str:
        return self.drive[0]

    @property
    def library_url(self) -> str:
        return self.drive[1]

    def container_id(self, create: bool = False) -> str | None:
        if self._container_id is None:
            found = self.graph.find_item(self.drive_id, None, self.plan.container)
            if found is None and create:
                found = self.graph.create_folder(self.drive_id, None, self.plan.container)
                self.event("folder_created", path=self.plan.container)
            if found is not None:
                self._container_id = str(found["id"])
        return self._container_id

    def folder_id(self, path: str) -> str | None:
        """Id of a spec folder inside the container (None if it does not exist)."""
        if path in self._folder_ids:
            return self._folder_ids[path]
        parent = self.container_id()
        if parent is None:
            return None
        for segment in path.split("/"):
            found = self.graph.find_item(self.drive_id, parent, segment)
            if found is None:
                return None
            parent = str(found["id"])
        self._folder_ids[path] = parent
        return parent

    def folder_item(self, path: str) -> str:
        return folder_item_path(f"{self.library_url}/{self.plan.container}/{path}")

    def file_item(self, folder_path: str, name: str) -> str:
        return file_item_path(f"{self.library_url}/{self.plan.container}/{folder_path}/{name}")

    def special_file_name(self, file_id: str) -> str:
        return next(s.doc.name for s in self.plan.special_files if s.id == file_id)

    def item_path(self, target: str) -> str:
        """REST item path for an oracle target: "root", a spec folder path or "special:<id>"."""
        if target == "root":
            return "/web"
        if target.startswith("special:"):
            file_id = target[len("special:") :]
            if file_id not in self.special_folder:
                raise GeneratorError(f"unknown special file {file_id!r}")
            return self.file_item(self.special_folder[file_id], self.special_file_name(file_id))
        if target not in {f.path for f in self.plan.folders}:
            raise GeneratorError(f"unknown folder {target!r}")
        return self.folder_item(target)

    # -- ids ------------------------------------------------------------------------------------

    def user_id(self, spec_id: str) -> str:
        if spec_id not in self._user_ids:
            user = next((u for u in self.plan.users if u.id == spec_id), None)
            if user is None:
                raise GeneratorError(f"user {spec_id!r} is not generated by this spec")
            found = self.graph.get_user(user.upn)
            if found is None:
                raise GeneratorError(f"user {user.upn!r} does not exist (run generate first)")
            self._user_ids[spec_id] = str(found["id"])
        return self._user_ids[spec_id]

    def group_id(self, spec_id: str) -> str:
        if spec_id not in self._group_ids:
            group = next((g for g in self.plan.groups if g.id == spec_id), None)
            if group is None:
                raise GeneratorError(f"group {spec_id!r} is not in the spec")
            found = self.graph.find_group(group.name)
            if found is None:
                raise GeneratorError(f"group {group.name!r} does not exist (run generate first)")
            self._group_ids[spec_id] = str(found["id"])
        return self._group_ids[spec_id]

    def remember_user(self, spec_id: str, object_id: str) -> None:
        self._user_ids[spec_id] = object_id

    def remember_group(self, spec_id: str, object_id: str) -> None:
        self._group_ids[spec_id] = object_id

    def remember_folder(self, path: str, item_id: str) -> None:
        self._folder_ids[path] = item_id

    # -- SharePoint principals and roles -----------------------------------------------------------

    def principal(self, kind: str, ref: str) -> Principal:
        """The SharePoint principal of a spec user or group (ensureuser, retried if not found)."""
        key = (kind, ref)
        if key not in self._principals:
            if kind == "user":
                user = next((u for u in self.plan.users if u.id == ref), None)
                if user is None:
                    raise GeneratorError(f"user {ref!r} is not generated by this spec")
                logon = user.upn
            elif kind == "entra_group":
                logon = group_claim(self.group_id(ref))
            else:
                raise GeneratorError(f"unknown principal kind {kind!r}")
            data = self.retry(
                lambda: self.sp.ensure_principal(logon),
                is_ensureuser_not_found,
                self.settings.ensureuser_timeout_s,
            )
            self._principals[key] = Principal(str(data["LoginName"]), int(data["Id"]))
        return self._principals[key]

    def login_without_side_effects(self, grant: GrantPlan) -> str:
        """The login a grant will have, computed without calling ensureuser (used by verify)."""
        if grant.kind == "user":
            user = next(u for u in self.plan.users if u.id == grant.ref)
            return user_login(user.upn)
        return group_claim(self.group_id(grant.ref))

    @property
    def roles(self) -> dict[str, int]:
        if self._roles is None:
            self._roles = self.sp.role_definitions()
        return self._roles

    def role_id(self, role_name: str) -> int:
        if role_name not in self.roles:
            raise GeneratorError(f"role {role_name!r} not found among {sorted(self.roles)}")
        return self.roles[role_name]

    def add_member(self, group_id: str, member_id: str, member_is_group: bool) -> bool:
        """Add a member; retry the 404 seen right after a group is created; "already" is success."""
        return bool(
            self.retry(
                lambda: self.graph.add_member(group_id, member_id, member_is_group),
                is_member_add_not_ready,
                self.settings.member_add_timeout_s,
            )
        )
