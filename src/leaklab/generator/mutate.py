"""apply_mutation(): change the tenant after generation and log WHEN the change was confirmed.

Each mutation is executed through the verified calls, then Entra/REST is polled until it shows the
change (requested_at -> executed_at -> confirmed_at in epoch seconds). The log entry's
`confirmed_at` is what the oracle takes as the moment intent changes. Confirmation is about
STRUCTURE only (the directory and SharePoint say the change happened); it says nothing about when
SharePoint starts or stops serving content, which the scenario runner measures.

Supported: grant_added, grant_removed, group_member_added, group_member_removed,
inheritance_broken, file_moved. NOT supported: inheritance_restored (resetting inheritance has not
been verified), so it raises instead of guessing.

Confirmation per type: grants and inheritance through REST (role assignments, HasUnique...);
file_moved through the PATCH response (the new parent id); user membership through the user's
transitive groups, re-read until two consecutive reads agree (that read can flap); group-in-group
membership through a witness user who is (per the spec) inside the child group, or, if there is no
such user, by the call's success alone (noted in the entry).
"""

from collections.abc import Callable

from leaklab.generator.env import LIMITED_ACCESS, Env
from leaklab.generator.log import LogEntry
from leaklab.generator.plan import GeneratorError
from leaklab.generator.verify import expected_group_ids
from leaklab.graph_client import GraphError
from leaklab.oracle import Mutation, MutationType
from leaklab.sp_rest import SPRestError

Check = Callable[[], bool]


def _has(env: Env, item: str, login: str, role: str | None) -> bool:
    for a in env.sp.assignments(item):
        if a.login == login and any(
            (role is None or r.name == role) and r.kind != 1 and r.name != LIMITED_ACCESS
            for r in a.roles
        ):
            return True
    return False


def _grant_added(env: Env, m: Mutation) -> tuple[Check, str]:
    p = m.params
    item = env.item_path(str(p["target"]))
    if p["target"] != "root" and not env.sp.has_unique(item):
        raise GeneratorError(f"{p['target']!r} inherits: break inheritance before adding a grant")
    principal = env.principal(str(p["principal_kind"]), str(p["principal_ref"]))
    role = {"read": "Read", "full_control": "Full Control"}.get(str(p["role"]))
    if role is None:
        raise GeneratorError(f"role {p['role']!r} has no SharePoint mapping")
    env.sp.add_assignment(item, principal.sp_id, env.role_id(role))
    return (lambda: _has(env, item, principal.login, role)), ""


def _grant_removed(env: Env, m: Mutation) -> tuple[Check, str]:
    p = m.params
    item = env.item_path(str(p["target"]))
    principal = env.principal(str(p["principal_kind"]), str(p["principal_ref"]))
    role = (
        {"read": "Read", "full_control": "Full Control"}.get(str(p["role"]))
        if p.get("role")
        else None
    )
    removed = 0
    for a in env.sp.assignments(item):
        if a.login != principal.login:
            continue
        for r in a.roles:
            if r.kind == 1 or r.name == LIMITED_ACCESS or (role is not None and r.name != role):
                continue
            env.sp.remove_assignment(item, a.principal_id, r.id)
            removed += 1
    return (
        lambda: not _has(env, item, principal.login, role)
    ), f"removed {removed} role binding(s)"


def _witness(env: Env, group_spec_id: str) -> str | None:
    """A generated user who is inside `group_spec_id` according to the spec."""
    return next(
        (uid for uid, groups in expected_group_ids(env).items() if group_spec_id in groups), None
    )


def _member(env: Env, m: Mutation, add: bool) -> tuple[Check, str]:
    p = m.params
    group_id = env.group_id(str(p["group"]))
    is_group = p["member_kind"] == "entra_group"
    member_id = (
        env.group_id(str(p["member_ref"])) if is_group else env.user_id(str(p["member_ref"]))
    )
    if add:
        env.add_member(group_id, member_id, is_group)
    else:
        try:
            env.graph.remove_member(group_id, member_id)
        except GraphError as exc:
            if exc.status_code != 404:  # 404: already not a member
                raise
    witness = _witness(env, str(p["member_ref"])) if is_group else str(p["member_ref"])
    if witness is None:
        return (
            lambda: True
        ), "no witness user inside the child group: confirmed by the call result only"
    witness_id = env.user_id(witness)
    return (
        lambda: (group_id in env.graph.user_group_ids(witness_id)) == add
    ), f"witness user {witness}"


def _broken(env: Env, m: Mutation) -> tuple[Check, str]:
    item = env.item_path(str(m.params["target"]))
    if m.params["target"] == "root":
        raise GeneratorError("the site root has no parent to break inheritance from")
    env.sp.break_inheritance(
        item, copy_role_assignments=bool(m.params.get("copy_role_assignments"))
    )
    return (lambda: env.sp.has_unique(item)), ""


def _moved(env: Env, m: Mutation) -> tuple[Check, str]:
    file_id = str(m.params["file"])
    destination = str(m.params["to_folder"])
    if file_id not in env.special_folder:
        raise GeneratorError(f"unknown special file {file_id!r}")
    source_folder = env.folder_id(env.special_folder[file_id])
    dest_id = env.folder_id(destination)
    if source_folder is None or dest_id is None:
        raise GeneratorError("source or destination folder does not exist")
    item = env.graph.find_item(env.drive_id, source_folder, env.special_file_name(file_id))
    if item is None:
        raise GeneratorError(f"special file {file_id!r} is not in {env.special_folder[file_id]!r}")
    moved = env.graph.move_item(env.drive_id, str(item["id"]), dest_id)
    env.special_folder[file_id] = destination
    parent = str(moved.get("parentReference", {}).get("id", ""))
    return (lambda: parent == dest_id), ""


_EXECUTORS: dict[MutationType, Callable[[Env, Mutation], tuple[Check, str]]] = {
    MutationType.GRANT_ADDED: _grant_added,
    MutationType.GRANT_REMOVED: _grant_removed,
    MutationType.GROUP_MEMBER_ADDED: lambda env, m: _member(env, m, True),
    MutationType.GROUP_MEMBER_REMOVED: lambda env, m: _member(env, m, False),
    MutationType.INHERITANCE_BROKEN: _broken,
    MutationType.FILE_MOVED: _moved,
}


def _confirm(env: Env, check: Check, consecutive: int) -> float | None:
    """Poll `check` until it is true `consecutive` times in a row; the time of the first of them."""
    deadline = env.clock() + env.settings.confirm_timeout_s
    first: float | None = None
    streak = 0
    while True:
        now = env.clock()
        if check():
            streak += 1
            first = first if first is not None else now
            if streak >= consecutive:
                return first
        else:
            streak, first = 0, None
        if env.clock() >= deadline:
            return None
        env.sleep(env.settings.confirm_interval_s)


def apply_mutation(env: Env, mutation: Mutation) -> LogEntry:
    """Execute one mutation, confirm it, and append it to the mutation log."""
    requested = env.clock()
    executor = _EXECUTORS.get(mutation.type)
    requested_copy = mutation.model_copy(update={"confirmed_at": None})
    seq = len(env.log.entries) + 1
    if executor is None:
        entry = LogEntry(
            seq=seq,
            mutation=requested_copy,
            requested_at=requested,
            status="failed",
            note=f"{mutation.type.value} is not supported (not verified)",
        )
        env.log.add(entry)
        raise GeneratorError(entry.note)
    try:
        check, note = executor(env, mutation)
    except (GeneratorError, GraphError, SPRestError) as exc:
        env.log.add(
            LogEntry(
                seq=seq,
                mutation=requested_copy,
                requested_at=requested,
                status="failed",
                note=str(exc),
            )
        )
        raise
    executed = env.clock()
    is_membership = mutation.type in (
        MutationType.GROUP_MEMBER_ADDED,
        MutationType.GROUP_MEMBER_REMOVED,
    )
    confirmed = _confirm(env, check, consecutive=2 if is_membership else 1)
    entry = LogEntry(
        seq=seq,
        mutation=requested_copy,
        requested_at=requested,
        executed_at=executed,
        confirmed_at=confirmed,
        status="confirmed" if confirmed is not None else "unconfirmed",
        note=note,
    )
    env.log.add(entry)
    env.event("mutation", type=mutation.type.value, status=entry.status, confirmed_at=confirmed)
    return entry
