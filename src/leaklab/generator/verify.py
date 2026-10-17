"""verify(): compare the real tenant with the spec. SharePoint structure is read through REST.

Checks: users exist (and licensed users hold the license: only a warning, the license list lags
~13s), groups exist, every user is in exactly the groups the spec implies (read through Graph's
transitiveMemberOf, re-read until it settles because that read lags ~12-24s), the container, folders
and documents exist, each folder / special file has unique permissions exactly when the spec says so
and exactly the spec's grants (Limited Access ignored: it is added by SharePoint and is not read
access), the root grants are present, and the site's Members/Visitors groups are empty.

verify() has no side effects: it never calls ensureuser or creates anything. It checks STRUCTURE
(what the directory and SharePoint say they contain). It says nothing about what SharePoint will
serve to a user right now: that is what the scenario runner measures.
"""

from collections.abc import Callable, Iterable
from typing import Literal

from pydantic import BaseModel

from leaklab.generator.env import LIMITED_ACCESS, Env, user_login
from leaklab.generator.plan import GrantPlan
from leaklab.generator.ports import Assignment


class Issue(BaseModel):
    severity: Literal["error", "warning"]
    what: str
    expected: str = ""
    actual: str = ""


class VerifyReport(BaseModel):
    ok: bool
    issues: list[Issue]
    checked: dict[str, int]


def compare_assignments(
    desired: Iterable[tuple[str, str]], actual: Iterable[Assignment]
) -> tuple[set[tuple[str, str]], set[tuple[str, str]]]:
    """(missing, extra) as (login, role name) sets; Limited Access entries are not counted."""
    have = {
        (a.login, r.name)
        for a in actual
        for r in a.roles
        if r.kind != 1 and r.name != LIMITED_ACCESS
    }
    want = set(desired)
    return want - have, have - want


def expected_group_ids(env: Env) -> dict[str, set[str]]:
    """spec user id -> spec group ids the user belongs to, directly or through nesting."""
    parents: dict[str, set[str]] = {g.id: set() for g in env.plan.groups}
    for g in env.plan.groups:
        for sub in g.group_members:
            parents[sub].add(g.id)  # `sub` is inside `g`
    result: dict[str, set[str]] = {}
    for user in env.plan.users:
        groups = {g.id for g in env.plan.groups if user.id in g.user_members}
        frontier = list(groups)
        while frontier:
            for parent in parents[frontier.pop()]:
                if parent not in groups:
                    groups.add(parent)
                    frontier.append(parent)
        result[user.id] = groups
    return result


def _grant_pairs(env: Env, grants: Iterable[GrantPlan]) -> set[tuple[str, str]]:
    return {(env.login_without_side_effects(g), g.role_name) for g in grants}


def verify(env: Env) -> VerifyReport:
    issues: list[Issue] = []
    checked = {
        "users": 0,
        "groups": 0,
        "sp_groups": 0,
        "folders": 0,
        "documents": 0,
        "unique_items": 0,
    }

    def issue(
        severity: Literal["error", "warning"], what: str, expected: str = "", actual: str = ""
    ) -> None:
        issues.append(Issue(severity=severity, what=what, expected=expected, actual=actual))

    # users and licenses
    sku_known = any(u.licensed for u in env.plan.users)
    for user in env.plan.users:
        checked["users"] += 1
        found = env.graph.get_user(user.upn)
        if found is None:
            issue("error", f"user {user.upn} is missing")
            continue
        env.remember_user(user.id, str(found["id"]))
        if user.licensed and sku_known and not env.graph.user_license_sku_ids(str(found["id"])):
            issue("warning", f"licensed user {user.upn} shows no license yet (the list can lag)")

    # groups
    for group in env.plan.groups:
        checked["groups"] += 1
        found_group = env.graph.find_group(group.name)
        if found_group is None:
            issue("error", f"group {group.name} is missing")
        else:
            env.remember_group(group.id, str(found_group["id"]))

    # memberships (as seen through each user's transitive group list)
    if not any(i.severity == "error" for i in issues):
        wanted = expected_group_ids(env)
        for user in env.plan.users:
            want_ids = {env.group_id(g) for g in wanted[user.id]}
            ours = {env.group_id(g.id) for g in env.plan.groups}
            uid = env.user_id(user.id)
            deadline = env.clock() + env.settings.membership_settle_s
            while True:
                actual = env.graph.user_group_ids(uid) & ours
                if actual == want_ids or env.clock() >= deadline:
                    break
                env.sleep(env.settings.retry_interval_s)
            if actual != want_ids:
                issue(
                    "error",
                    f"group membership of {user.upn}",
                    expected=str(sorted(wanted[user.id])),
                    actual=str(
                        sorted(g.id for g in env.plan.groups if env.group_id(g.id) in actual)
                    ),
                )

    # SharePoint groups and their members (REST)
    for sp_plan in env.plan.sp_groups:
        checked["sp_groups"] += 1
        found_sp = env.sp.find_sp_group(sp_plan.title)
        if found_sp is None:
            issue("error", f"SharePoint group {sp_plan.title} is missing")
            continue
        env.remember_sp_group(sp_plan.id, str(found_sp["LoginName"]), int(found_sp["Id"]))
        have = set(env.sp.sp_group_users(int(found_sp["Id"])))
        want = {
            user_login(next(u.upn for u in env.plan.users if u.id == uid))
            for uid in sp_plan.user_members
        }
        if have != want:
            issue(
                "error",
                f"members of SharePoint group {sp_plan.title}",
                expected=str(sorted(want)),
                actual=str(sorted(have)),
            )

    # folders, documents, permissions
    if env.container_id() is None:
        issue("error", f"container folder {env.plan.container} is missing")
    else:
        for folder in env.plan.folders:
            checked["folders"] += 1
            folder_id = env.folder_id(folder.path)
            if folder_id is None:
                issue("error", f"folder {folder.path} is missing")
                continue
            for doc in folder.docs:
                checked["documents"] += 1
                if env.graph.find_item(env.drive_id, folder_id, doc.name) is None:
                    issue("error", f"document {folder.path}/{doc.name} is missing")
            _check_item(
                env,
                env.folder_item(folder.path),
                folder.path,
                folder.inherits,
                folder.grants,
                issue,
                checked,
            )
        for special in env.plan.special_files:
            folder_id = env.folder_id(special.folder)
            if (
                folder_id is None
                or env.graph.find_item(env.drive_id, folder_id, special.doc.name) is None
            ):
                issue("error", f"special file {special.id} ({special.doc.name}) is missing")
                continue
            checked["documents"] += 1
            _check_item(
                env,
                env.file_item(special.folder, special.doc.name),
                f"special file {special.id}",
                special.inherits,
                special.grants,
                issue,
                checked,
            )
        root = env.sp.assignments("/web")
        missing, _ = compare_assignments(_grant_pairs(env, env.plan.root_grants), root)
        for login, role in sorted(missing):
            issue("error", "site-root grant is missing", expected=f"{login} -> {role}")

    for kind in ("member", "visitor"):
        members = env.sp.default_group_members(kind)
        if members:
            issue("warning", f"the site's {kind}s group is not empty", actual=str(members))

    return VerifyReport(
        ok=not any(i.severity == "error" for i in issues), issues=issues, checked=checked
    )


def _check_item(
    env: Env,
    item: str,
    label: str,
    inherits: bool,
    grants: Iterable[GrantPlan],
    issue: Callable[..., None],
    checked: dict[str, int],
) -> None:
    unique = env.sp.has_unique(item)
    if unique == inherits:
        issue(
            "error",
            f"{label}: permission inheritance differs from the spec",
            expected="inherits" if inherits else "unique permissions",
            actual="unique permissions" if unique else "inherits",
        )
        return
    if inherits:
        return
    checked["unique_items"] += 1
    missing, extra = compare_assignments(_grant_pairs(env, grants), env.sp.assignments(item))
    for login, role in sorted(missing):
        issue("error", f"{label}: grant is missing", expected=f"{login} -> {role}")
    for login, role in sorted(extra):
        issue("error", f"{label}: unexpected grant", actual=f"{login} -> {role}")
