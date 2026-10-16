"""teardown(): remove what generate() created, by prefix only.

Only objects whose names the plan derives from the spec's prefix are touched, and each name is
checked with assert_prefixed() before anything is deleted: the container folder "<prefix>lab"
(everything generated lives inside it), the Entra groups "<prefix><id>", the users
"<prefix><id>@domain", and the site-root role assignments of our own principals. The admin account,
the site's own groups and any other folder are never touched.

Order: site-root assignments of our principals first (while the groups still exist, so none is
left orphaned), then the container, then groups, then users. A 404 on a delete means "already gone".

Not done (not verified): removing the SharePoint site users and their "Limited Access" entries that
SharePoint keeps after a group or user is deleted. They remain; they confer no read access.
"""

from collections import Counter
from functools import partial

from pydantic import BaseModel

from leaklab.generator.env import LIMITED_ACCESS, Env, user_login
from leaklab.generator.plan import GeneratorError, assert_prefixed
from leaklab.generator.retry import gone_ok


class TeardownReport(BaseModel):
    counts: dict[str, int]
    events: list[dict[str, object]]


def teardown(env: Env) -> TeardownReport:
    prefix = env.plan.prefix
    counts: Counter[str] = Counter()

    # 1. site-root assignments of our principals (read-only lookup of ids: nothing is created)
    root = env.sp.assignments("/web")
    for grant in env.plan.root_grants:
        try:
            login = (
                user_login(next(u.upn for u in env.plan.users if u.id == grant.ref))
                if grant.kind == "user"
                else env.login_without_side_effects(grant)
            )
        except GeneratorError:
            continue  # the group or user is already gone
        for a in root:
            if a.login != login:
                continue
            for role in a.roles:
                if role.kind == 1 or role.name == LIMITED_ACCESS:
                    continue
                env.sp.remove_assignment("/web", a.principal_id, role.id)
                counts["root_assignments_removed"] += 1
                env.event("root_grant_removed", login=login, role=role.name)

    # 2. the container folder (and with it every generated folder and document)
    assert_prefixed(env.plan.container, prefix)
    container = env.container_id()
    if container is not None:
        if gone_ok(lambda: env.graph.delete_item(env.drive_id, container)):
            counts["container_deleted"] += 1
            env.event("folder_deleted", path=env.plan.container)

    # 3. groups, then 4. users
    for group in env.plan.groups:
        assert_prefixed(group.name, prefix)
        found = env.graph.find_group(group.name)
        if found is not None and gone_ok(partial(env.graph.delete_group, str(found["id"]))):
            counts["groups_deleted"] += 1
            env.event("group_deleted", name=group.name)
    for user in env.plan.users:
        assert_prefixed(user.upn, prefix)
        found_user = env.graph.get_user(user.upn)
        if found_user is not None and gone_ok(
            partial(env.graph.delete_user, str(found_user["id"]))
        ):
            counts["users_deleted"] += 1
            env.event("user_deleted", upn=user.upn)
    return TeardownReport(counts=dict(counts), events=list(env.events))
