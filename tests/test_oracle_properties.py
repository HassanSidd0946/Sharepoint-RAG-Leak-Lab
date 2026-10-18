"""Property-based tests (Hypothesis) for the oracle on random small tenants.

Every property compares the oracle with an INDEPENDENT reference written here on plain dicts:
group expansion goes top-down (the oracle goes bottom-up), inheritance is a plain recursion, and
mutations are applied by editing the dict. The reference shares no code with leaklab.oracle.
"""

import copy
from typing import Any

from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from leaklab.oracle import Mutation, MutationType, expected_access
from leaklab.spec import TenantSpec, generate_canaries, special_key

SETTINGS = settings(
    max_examples=60,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
READ = {"read", "edit", "full_control"}


# -- random tenants ------------------------------------------------------------------------------


@st.composite
def spec_dicts(draw: st.DrawFn) -> dict[str, Any]:
    n_users = draw(st.integers(2, 5))
    n_groups = draw(st.integers(1, 4))
    user_ids = [f"u{i}" for i in range(n_users)]
    group_ids = [f"g{i}" for i in range(n_groups)]
    users = [
        {"id": u, "display": u.upper(), "persona": True, "licensed": False} for u in user_ids
    ] + [{"id": "admin", "display": "Admin", "persona": False, "licensed": True}]
    groups = []
    for i, gid in enumerate(group_ids):
        members = draw(st.lists(st.sampled_from(user_ids), unique=True, max_size=3))
        later = group_ids[i + 1 :]  # nesting only towards later groups: no cycles in the spec
        subs = draw(st.lists(st.sampled_from(later), unique=True, max_size=2)) if later else []
        groups.append({"id": gid, "members": members, "member_groups": subs})

    sp_ids = [f"s{i}" for i in range(draw(st.integers(0, 2)))]
    sp_groups = [
        {"id": s, "members": draw(st.lists(st.sampled_from(user_ids), unique=True, max_size=3))}
        for s in sp_ids
    ]
    principals = (
        [("user", u) for u in user_ids]
        + [("entra_group", g) for g in group_ids]
        + [("sp_group", s) for s in sp_ids]
    )

    def grants(min_size: int, max_size: int) -> list[dict[str, Any]]:
        chosen = draw(
            st.lists(st.sampled_from(principals), min_size=min_size, max_size=max_size, unique=True)
        )
        return [
            {
                "principal": {"kind": kind, "ref": ref},
                "role": draw(st.sampled_from(["read", "full_control"])),
            }
            for kind, ref in chosen
        ]

    folders: list[dict[str, Any]] = []
    for i in range(draw(st.integers(1, 3))):
        path = f"F{i}"
        inherits = draw(st.booleans())
        folders.append(
            {
                "path": path,
                "inherits": inherits,
                "grants": [] if inherits else grants(1, 2),
                "doc_count": draw(st.integers(1, 2)),
            }
        )
        if draw(st.booleans()):
            inherits_child = draw(st.booleans())
            folders.append(
                {
                    "path": f"{path}/c",
                    "inherits": inherits_child,
                    "grants": [] if inherits_child else grants(1, 2),
                    "doc_count": 1,
                }
            )
    special: list[dict[str, Any]] = []
    if draw(st.booleans()):
        inherits_file = draw(st.booleans())
        special.append(
            {
                "id": "f0",
                "folder": draw(st.sampled_from([f["path"] for f in folders])),
                "name": "f0.txt",
                "inherits": inherits_file,
                "grants": [] if inherits_file else grants(1, 2),
            }
        )
    return {
        "version": 1,
        "naming": {"prefix": "ll-", "domain": "t.onmicrosoft.com", "seed": 7},
        "site": {
            "title": "T",
            "url": "https://t.sharepoint.com/sites/T",
            "library": "Documents",
            "root_grants": grants(0, 2),
        },
        "users": users,
        "groups": groups,
        "sp_groups": sp_groups,
        "folders": folders,
        "special_files": special,
        "documents": {
            "format": "txt",
            "canary_pattern": "CANARY-{generation}-{folder_code}-{index:02d}-{hex6}",
            "special_canary_pattern": "CANARY-{generation}-SPC-{hex6}",
            "folder_codes": {f["path"]: f"C{i}" for i, f in enumerate(folders)},
            "body_template": "{title}",
            "topics": ["t"],
        },
        "policy": {
            "org_wide_sharing_link": "x",
            "unknown_principal": "deny",
            "guest_users": "deny_unless_explicit",
        },
        "scenarios": [],
    }


# -- the independent reference ------------------------------------------------------------------


def ref_group_users(d: dict[str, Any], gid: str, trail: tuple[str, ...] = ()) -> set[str]:
    """Every user in a group, expanding nested groups top-down."""
    group = next(g for g in d["groups"] if g["id"] == gid)
    users = set(group["members"])
    for sub in group["member_groups"]:
        if sub not in trail:
            users |= ref_group_users(d, sub, (*trail, gid))
    return users


def ref_folder_origin(d: dict[str, Any], path: str) -> str:
    """Where a folder's permissions come from: "root", or the path of the nearest unique folder."""
    folder = next(f for f in d["folders"] if f["path"] == path)
    if not folder["inherits"]:
        return path
    parent = path.rpartition("/")[0]
    return ref_folder_origin(d, parent) if parent else "root"


def ref_grants_of(d: dict[str, Any], origin: str) -> list[dict[str, Any]]:
    if origin == "root":
        return list(d["site"]["root_grants"])
    if origin.startswith("special:"):
        return list(next(s for s in d["special_files"] if s["id"] == origin[8:])["grants"])
    return list(next(f for f in d["folders"] if f["path"] == origin)["grants"])


def ref_allows(d: dict[str, Any], user: str, grants: list[dict[str, Any]]) -> bool:
    for grant in grants:
        if grant["role"] not in READ:
            continue
        who = grant["principal"]
        if who["kind"] == "user" and who["ref"] == user:
            return True
        if who["kind"] == "entra_group" and user in ref_group_users(d, who["ref"]):
            return True
        if who["kind"] == "sp_group" and user in next(
            sg["members"] for sg in d["sp_groups"] if sg["id"] == who["ref"]
        ):
            return True
    return False


def ref_file_origin(d: dict[str, Any], sf: dict[str, Any]) -> str:
    return ref_folder_origin(d, sf["folder"]) if sf["inherits"] else f"special:{sf['id']}"


def ref_readable(d: dict[str, Any], canaries: dict[str, list[str]]) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for user in d["users"]:
        if not user["persona"]:
            continue
        got: set[str] = set()
        for folder in d["folders"]:
            grants = ref_grants_of(d, ref_folder_origin(d, folder["path"]))
            if ref_allows(d, user["id"], grants):
                got |= set(canaries[folder["path"]])
        for sf in d["special_files"]:
            if ref_allows(d, user["id"], ref_grants_of(d, ref_file_origin(d, sf))):
                got |= set(canaries[special_key(sf["id"])])
        out[user["id"]] = got
    return out


def ref_scope_canaries(d: dict[str, Any], target: str, canaries: dict[str, list[str]]) -> set[str]:
    """Canaries whose permissions come from `target` (itself or by inheriting through it)."""
    scope: set[str] = set()
    for folder in d["folders"]:
        if ref_folder_origin(d, folder["path"]) == target:
            scope |= set(canaries[folder["path"]])
    for sf in d["special_files"]:
        if ref_file_origin(d, sf) == target:
            scope |= set(canaries[special_key(sf["id"])])
    return scope


# -- helpers -------------------------------------------------------------------------------------


def build(d: dict[str, Any]) -> tuple[TenantSpec, dict[str, list[str]]]:
    spec = TenantSpec.model_validate(d)
    return spec, generate_canaries(spec)


def unique_targets(d: dict[str, Any]) -> list[str]:
    """Targets whose grants may be edited: the root and every item with its own permissions."""
    targets = ["root"] + [f["path"] for f in d["folders"] if not f["inherits"]]
    return targets + [f"special:{s['id']}" for s in d["special_files"] if not s["inherits"]]


def target_grants(d: dict[str, Any], target: str) -> list[dict[str, Any]]:
    """The live grants list of a target inside `d` (edited in place by the tests)."""
    if target == "root":
        return d["site"]["root_grants"]  # type: ignore[no-any-return]
    if target.startswith("special:"):
        return next(s for s in d["special_files"] if s["id"] == target[8:])["grants"]  # type: ignore[no-any-return]
    return next(f for f in d["folders"] if f["path"] == target)["grants"]  # type: ignore[no-any-return]


def principals_of(d: dict[str, Any]) -> list[tuple[str, str]]:
    return (
        [("user", u["id"]) for u in d["users"] if u["persona"]]
        + [("entra_group", g["id"]) for g in d["groups"]]
        + [("sp_group", s["id"]) for s in d["sp_groups"]]
    )


def add_grant(target: str, kind: str, ref: str, at: float = 1.0) -> Mutation:
    return Mutation(
        type=MutationType.GRANT_ADDED,
        params={
            "target": target,
            "principal_kind": kind,
            "principal_ref": ref,
            "role": "read",
        },
        confirmed_at=at,
    )


def remove_grant(target: str, kind: str, ref: str, at: float = 1.0) -> Mutation:
    return Mutation(
        type=MutationType.GRANT_REMOVED,
        params={"target": target, "principal_kind": kind, "principal_ref": ref},
        confirmed_at=at,
    )


def subset(before: dict[str, set[str]], after: dict[str, set[str]]) -> bool:
    return all(before[p] <= after[p] for p in before)


# -- properties ---------------------------------------------------------------------------------


@SETTINGS
@given(d=spec_dicts())
def test_nesting_and_inheritance_match_the_brute_force_reference(d: dict[str, Any]) -> None:
    spec, canaries = build(d)
    assert expected_access(spec, [], 0.0) == ref_readable(d, canaries)


@SETTINGS
@given(d=spec_dicts(), data=st.data())
def test_adding_a_grant_never_removes_access(d: dict[str, Any], data: st.DataObject) -> None:
    spec, canaries = build(d)
    target = data.draw(st.sampled_from(unique_targets(d)))
    kind, ref = data.draw(st.sampled_from(principals_of(d)))
    before = expected_access(spec, [], 2.0)
    after = expected_access(spec, [add_grant(target, kind, ref)], 2.0)
    assert subset(before, after)
    edited = copy.deepcopy(d)
    target_grants(edited, target).append({"principal": {"kind": kind, "ref": ref}, "role": "read"})
    assert after == ref_readable(edited, canaries)  # and it gains exactly what the reference says


@SETTINGS
@given(d=spec_dicts(), data=st.data())
def test_revoking_a_grant_removes_exactly_the_expected_canaries(
    d: dict[str, Any], data: st.DataObject
) -> None:
    spec, canaries = build(d)
    candidates = [(t, g) for t in unique_targets(d) for g in target_grants(d, t)]
    assume(candidates)
    target, grant = data.draw(st.sampled_from(candidates))
    kind, ref = grant["principal"]["kind"], grant["principal"]["ref"]
    before = expected_access(spec, [], 2.0)
    after = expected_access(spec, [remove_grant(target, kind, ref)], 2.0)

    edited = copy.deepcopy(d)
    target_grants(edited, target)[:] = [
        g
        for g in target_grants(edited, target)
        if not (g["principal"]["kind"] == kind and g["principal"]["ref"] == ref)
    ]
    reference_before, reference_after = ref_readable(d, canaries), ref_readable(edited, canaries)
    scope = ref_scope_canaries(d, target, canaries)
    assert before == reference_before and after == reference_after
    for persona in before:
        assert after[persona] <= before[persona]  # revocation never adds access
        removed = before[persona] - after[persona]
        assert removed <= scope  # nothing outside the revoked item's scope changes
        assert removed == {
            c for c in scope if c in reference_before[persona] and c not in reference_after[persona]
        }


@SETTINGS
@given(d=spec_dicts(), data=st.data())
def test_group_membership_changes_are_monotone_and_match_the_reference(
    d: dict[str, Any], data: st.DataObject
) -> None:
    spec, canaries = build(d)
    group = data.draw(st.sampled_from([g["id"] for g in d["groups"]]))
    # an Entra group can contain users and Entra groups (not SharePoint groups)
    kind, ref = data.draw(st.sampled_from([p for p in principals_of(d) if p[0] != "sp_group"]))
    members_key = "members" if kind == "user" else "member_groups"
    before = expected_access(spec, [], 2.0)

    def mutation(t: MutationType) -> Mutation:
        return Mutation(
            type=t,
            params={"group": group, "member_kind": kind, "member_ref": ref},
            confirmed_at=1.0,
        )

    added = copy.deepcopy(d)
    entry = next(g for g in added["groups"] if g["id"] == group)
    if ref not in entry[members_key]:
        entry[members_key].append(ref)
    after_add = expected_access(spec, [mutation(MutationType.GROUP_MEMBER_ADDED)], 2.0)
    assert subset(before, after_add)
    assert after_add == ref_readable(added, canaries)

    removed = copy.deepcopy(d)
    entry = next(g for g in removed["groups"] if g["id"] == group)
    entry[members_key] = [m for m in entry[members_key] if m != ref]
    after_remove = expected_access(spec, [mutation(MutationType.GROUP_MEMBER_REMOVED)], 2.0)
    assert subset(after_remove, before)
    assert after_remove == ref_readable(removed, canaries)


@SETTINGS
@given(d=spec_dicts(), data=st.data())
def test_moving_a_file_keeps_unique_grants_and_follows_the_folder_otherwise(
    d: dict[str, Any], data: st.DataObject
) -> None:
    assume(d["special_files"])
    spec, canaries = build(d)
    sf = d["special_files"][0]
    destination = data.draw(st.sampled_from([f["path"] for f in d["folders"]]))
    move = Mutation(
        type=MutationType.FILE_MOVED,
        params={"file": sf["id"], "to_folder": destination},
        confirmed_at=1.0,
    )
    before, after = expected_access(spec, [], 2.0), expected_access(spec, [move], 2.0)
    if not sf["inherits"]:
        assert after == before  # a moved file with its own grants keeps exactly them
    moved = copy.deepcopy(d)
    moved["special_files"][0]["folder"] = destination
    assert after == ref_readable(moved, canaries)


@SETTINGS
@given(d=spec_dicts(), data=st.data())
def test_the_result_does_not_depend_on_the_order_mutations_are_listed(
    d: dict[str, Any], data: st.DataObject
) -> None:
    spec, _ = build(d)
    targets = unique_targets(d)
    mutations: list[Mutation] = []
    for i in range(data.draw(st.integers(2, 4))):
        target = data.draw(st.sampled_from(targets))
        kind, ref = data.draw(st.sampled_from(principals_of(d)))
        maker = data.draw(st.sampled_from([add_grant, remove_grant]))
        mutations.append(maker(target, kind, ref, float(i + 1)))  # distinct confirmed times
    shuffled = data.draw(st.permutations(mutations))
    for t in (0.0, 1.5, 2.5, 10.0):
        assert expected_access(spec, mutations, t) == expected_access(spec, list(shuffled), t)


@SETTINGS
@given(d=spec_dicts(), data=st.data())
def test_adding_then_removing_a_new_grant_restores_the_original(
    d: dict[str, Any], data: st.DataObject
) -> None:
    spec, _ = build(d)
    target = data.draw(st.sampled_from(unique_targets(d)))
    kind, ref = data.draw(st.sampled_from(principals_of(d)))
    assume(
        not any(
            g["principal"]["kind"] == kind and g["principal"]["ref"] == ref
            for g in target_grants(d, target)
        )
    )
    both = [add_grant(target, kind, ref, 1.0), remove_grant(target, kind, ref, 2.0)]
    assert expected_access(spec, both, 3.0) == expected_access(spec, [], 3.0)


@SETTINGS
@given(d=spec_dicts(), data=st.data())
def test_removing_a_user_from_a_sharepoint_group_matches_the_reference(
    d: dict[str, Any], data: st.DataObject
) -> None:
    filled = [sg for sg in d["sp_groups"] if sg["members"]]
    assume(filled)
    sg = data.draw(st.sampled_from(filled))
    user = data.draw(st.sampled_from(sg["members"]))
    spec, canaries = build(d)
    removal = Mutation(
        type=MutationType.GROUP_MEMBER_REMOVED,
        params={"group": sg["id"], "member_kind": "user", "member_ref": user},
        confirmed_at=1.0,
    )
    after = expected_access(spec, [removal], 1.0)
    edited = copy.deepcopy(d)
    next(s for s in edited["sp_groups"] if s["id"] == sg["id"])["members"].remove(user)
    assert after == ref_readable(edited, canaries)
    assert subset(after, expected_access(spec, [], 1.0))  # removing a member never adds access
