"""generate(): build the tenant described by the spec, idempotently.

Order: users (+ licenses) -> Entra groups -> memberships -> SharePoint groups -> container folder,
folders, documents (Graph, content) -> permissions (SharePoint REST, structure). Every step looks
first and creates what is missing, so running it again changes nothing. On a re-run, an item
that should have its own permissions is brought to exactly the spec's grants (missing grants
added, extra non-"Limited Access" grants removed); an item that should INHERIT but is found
with its own permissions is reported as drift and stops the run (resetting inheritance has not
been verified).

Permissions: principals are resolved BEFORE inheritance is broken, then inheritance is broken and
the grants are added back to back, so an item is without grants for as short a time as possible.
"""

from collections import Counter
from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel

from leaklab.generator.env import LIMITED_ACCESS, Env
from leaklab.generator.plan import DocPlan, GeneratorError, GrantPlan
from leaklab.graph_client import GraphError


class GenerateReport(BaseModel):
    started_at: float
    finished_at: float
    counts: dict[str, int]
    warnings: list[str]
    skipped: list[str]
    events: list[dict[str, Any]]


def choose_sku(skus: Sequence[dict[str, Any]], part_number: str) -> str:
    """The license SKU id. With no part number, only an unambiguous tenant is auto-resolved."""
    rows: list[dict[str, Any]] = []
    for s in skus:
        prepaid = s.get("prepaidUnits", {}) or {}
        rows.append(
            {
                "part": s.get("skuPartNumber"),
                "id": str(s.get("skuId")),
                "applies": s.get("appliesTo"),
                "status": s.get("capabilityStatus"),
                "available": int(prepaid.get("enabled", 0)) - int(s.get("consumedUnits", 0)),
            }
        )
    if part_number:
        matches = [r for r in rows if r["part"] == part_number]
        if len(matches) != 1:
            raise GeneratorError(f"sku_part_number {part_number!r} matched {len(matches)} SKUs")
        return str(matches[0]["id"])
    candidates = [
        r
        for r in rows
        if r["applies"] == "User" and r["status"] == "Enabled" and r["available"] > 0
    ]
    if len(candidates) != 1:
        raise GeneratorError(
            f"{len(candidates)} SKUs could be assigned; set the SKU part number explicitly. "
            f"Candidates: {[c['part'] for c in candidates]}"
        )
    return str(candidates[0]["id"])


def _users(env: Env, counts: Counter[str]) -> None:
    sku_id: str | None = None
    if any(u.licensed for u in env.plan.users):
        if len(env.settings.usage_location) != 2:
            raise GeneratorError("a 2-letter usage location is required to assign licenses")
        sku_id = choose_sku(env.graph.list_skus(), env.settings.sku_part_number)
    for user in env.plan.users:
        found = env.graph.get_user(user.upn)
        if found is None:
            if not env.settings.password:
                raise GeneratorError("LEAKLAB_PERSONA_PASSWORD is required to create users")
            found = env.graph.create_user(
                user.upn,
                user.display,
                user.nickname,
                env.settings.password,
                env.settings.usage_location,
            )
            counts["users_created"] += 1
            env.event("user_created", upn=user.upn)
        else:
            counts["users_reused"] += 1
        object_id = str(found["id"])
        env.remember_user(user.id, object_id)
        if user.licensed and sku_id and sku_id not in env.graph.user_license_sku_ids(object_id):
            try:
                env.graph.assign_license(object_id, sku_id)
            except GraphError as exc:
                # UNVERIFIED: the exact error for assigning a license twice (the license list can
                # lag ~13s behind an assignment). Anything but a 400 mentioning "already" is raised.
                if not (exc.status_code == 400 and "already" in exc.message.lower()):
                    raise
            counts["licenses_assigned"] += 1
            env.event("license_assigned", upn=user.upn)


def _groups(env: Env, counts: Counter[str]) -> None:
    for group in env.plan.groups:
        found = env.graph.find_group(group.name)
        if found is None:
            found = env.graph.create_group(group.name)
            counts["groups_created"] += 1
            env.event("group_created", name=group.name)
        else:
            counts["groups_reused"] += 1
        env.remember_group(group.id, str(found["id"]))


def _memberships(env: Env, counts: Counter[str]) -> None:
    for group in env.plan.groups:
        gid = env.group_id(group.id)
        for user_id in group.user_members:
            added = env.add_member(gid, env.user_id(user_id), False)
            counts["members_added" if added else "members_already"] += 1
        for sub in group.group_members:
            added = env.add_member(gid, env.group_id(sub), True)
            counts["members_added" if added else "members_already"] += 1


def _sp_groups(env: Env, counts: Counter[str]) -> None:
    """SharePoint groups and their user members (create, find, add: all verified 2026-10-16)."""
    for group in env.plan.sp_groups:
        found = env.sp.find_sp_group(group.title)
        if found is None:
            found = env.sp.create_sp_group(group.title)
            counts["sp_groups_created"] += 1
            env.event("sp_group_created", title=group.title)
        else:
            counts["sp_groups_reused"] += 1
        gid = int(found["Id"])
        env.remember_sp_group(group.id, str(found["LoginName"]), gid)
        present = set(env.sp.sp_group_users(gid))
        for user_id in group.user_members:
            login = env.principal("user", user_id).login
            if login in present:
                counts["sp_members_already"] += 1
            else:
                env.sp.sp_group_add_user(gid, login)
                counts["sp_members_added"] += 1


def _ensure_file(env: Env, parent_id: str, doc: DocPlan, counts: Counter[str]) -> None:
    # An existing file is never overwritten: overwriting an item with its own permissions has not
    # been verified to keep them.
    if env.graph.find_item(env.drive_id, parent_id, doc.name) is None:
        env.graph.upload_text(env.drive_id, parent_id, doc.name, doc.text)
        counts["files_created"] += 1
    else:
        counts["files_reused"] += 1


def _content(env: Env, counts: Counter[str]) -> None:
    container = env.container_id(create=True)
    assert container is not None
    for folder in env.plan.folders:
        parent_path, _, leaf = folder.path.rpartition("/")
        parent_id = env.folder_id(parent_path) if parent_path else container
        if parent_id is None:
            raise GeneratorError(f"parent of folder {folder.path!r} is missing")
        found = env.graph.find_item(env.drive_id, parent_id, leaf)
        if found is None:
            found = env.graph.create_folder(env.drive_id, parent_id, leaf)
            counts["folders_created"] += 1
            env.event("folder_created", path=folder.path)
        else:
            counts["folders_reused"] += 1
        env.remember_folder(folder.path, str(found["id"]))
        for doc in folder.docs:
            _ensure_file(env, str(found["id"]), doc, counts)
    for special in env.plan.special_files:
        folder_id = env.folder_id(special.folder)
        if folder_id is None:
            raise GeneratorError(
                f"folder {special.folder!r} of special file {special.id!r} is missing"
            )
        _ensure_file(env, folder_id, special.doc, counts)


def apply_unique(
    env: Env, item: str, grants: Sequence[GrantPlan], label: str, counts: Counter[str]
) -> None:
    """Give an item exactly `grants` as its own permissions (break, then grant back to back)."""
    resolved = [(env.principal(g.kind, g.ref), g.role_name) for g in grants]  # before the break
    desired = {(p.login, role) for p, role in resolved}
    if not env.sp.has_unique(item):
        env.sp.break_inheritance(item, copy_role_assignments=False)
        counts["inheritance_broken"] += 1
        for principal, role in resolved:  # nothing is copied, so everything is added at once
            env.sp.add_assignment(item, principal.sp_id, env.role_id(role))
            counts["assignments_added"] += 1
        env.event("unique_permissions_applied", item=label, grants=len(resolved))
        return
    current = env.sp.assignments(item)
    have = {(a.login, r.name) for a in current for r in a.roles}
    for principal, role in resolved:
        if (principal.login, role) not in have:
            env.sp.add_assignment(item, principal.sp_id, env.role_id(role))
            counts["assignments_added"] += 1
    for a in current:
        for r in a.roles:
            if r.kind == 1 or r.name == LIMITED_ACCESS:
                continue  # SharePoint adds these itself; they are not read access
            if (a.login, r.name) not in desired:
                env.sp.remove_assignment(item, a.principal_id, r.id)
                counts["extra_assignments_removed"] += 1
                env.event("extra_assignment_removed", item=label, login=a.login, role=r.name)


def _permissions(env: Env, counts: Counter[str]) -> None:
    for folder in env.plan.folders:
        item = env.folder_item(folder.path)
        if folder.inherits:
            if env.sp.has_unique(item):
                raise GeneratorError(
                    f"drift: folder {folder.path!r} has its own permissions but the spec says it "
                    "inherits. Resetting inheritance is not verified: run teardown, then generate."
                )
        else:
            apply_unique(env, item, folder.grants, f"folder {folder.path}", counts)
    for special in env.plan.special_files:
        item = env.file_item(special.folder, special.doc.name)
        if special.inherits:
            if env.sp.has_unique(item):
                raise GeneratorError(
                    f"drift: special file {special.id!r} should inherit but does not"
                )
        else:
            apply_unique(env, item, special.grants, f"special file {special.id}", counts)
    root = env.sp.assignments("/web")
    for grant in env.plan.root_grants:
        principal = env.principal(grant.kind, grant.ref)
        if not any(
            a.login == principal.login and any(r.name == grant.role_name for r in a.roles)
            for a in root
        ):
            env.sp.add_assignment("/web", principal.sp_id, env.role_id(grant.role_name))
            counts["root_assignments_added"] += 1
            env.event("root_grant_added", login=principal.login, role=grant.role_name)


def generate(env: Env) -> GenerateReport:
    """Create (or complete) the tenant described by the spec. Safe to run repeatedly."""
    started = env.clock()
    counts: Counter[str] = Counter()
    _users(env, counts)
    _groups(env, counts)
    _memberships(env, counts)
    _sp_groups(env, counts)
    _content(env, counts)
    _permissions(env, counts)
    warnings = []
    for kind in ("member", "visitor"):
        members = env.sp.default_group_members(kind)
        if members:
            warnings.append(
                f"the site's {kind}s group is not empty ({len(members)}): the oracle assumes root "
                "access comes only from root_grants"
            )
    return GenerateReport(
        started_at=started,
        finished_at=env.clock(),
        counts=dict(counts),
        warnings=warnings,
        skipped=list(env.plan.skipped),
        events=env.events,
    )
