"""Tests for oracle.access on the real tenant_spec.yaml and on small variations of it."""

import copy
import logging
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from leaklab.oracle import (
    IntentTimeline,
    Mutation,
    MutationType,
    OracleError,
    expected_access,
)
from leaklab.spec import TenantSpec, generate_canaries, load_spec, special_key

REPO_SPEC = Path(__file__).resolve().parents[1] / "tenant_spec.yaml"
SPEC = load_spec(REPO_SPEC)
CAN = generate_canaries(SPEC)
SPECIAL = set(CAN[special_key("f-direct-share")])

GENERAL = set(CAN["General"])
ENGINEERING = set(CAN["Engineering"])
PLATFORM = set(CAN["Engineering/Platform-Secrets"])
FINANCE = set(CAN["Finance"])
LEADERSHIP = set(CAN["Finance/Leadership"])
HR = set(CAN["HR"])
BOARD = set(CAN["Board"])
S1_ENTRA = set(CAN["S1-Entra"])
S1_SPGROUP = set(CAN["S1-SPGroup"])
S1_DIRECT = set(CAN["S1-Direct"])
S1_ARMS = S1_ENTRA | S1_SPGROUP | S1_DIRECT  # p-plateng reads all three, by three kinds of grant
S2_NESTED = set(CAN["S2-Nested"])  # p-nest reads only this, through two nested groups


def mut(kind: MutationType, at: float | None = 1.0, **params: Any) -> Mutation:
    return Mutation(type=kind, params=params, confirmed_at=at)


def grant_added(target: str, kind: str, ref: str, role: str = "read", at: float = 1.0) -> Mutation:
    return mut(
        MutationType.GRANT_ADDED,
        at,
        target=target,
        principal_kind=kind,
        principal_ref=ref,
        role=role,
    )


def member_added(group: str, kind: str, ref: str, at: float = 1.0) -> Mutation:
    return mut(MutationType.GROUP_MEMBER_ADDED, at, group=group, member_kind=kind, member_ref=ref)


def spec_with(edit: Any) -> TenantSpec:
    """The real spec after `edit(dict)` has changed a deep copy of it."""
    with open(REPO_SPEC, encoding="utf-8") as fh:
        data: dict[str, Any] = copy.deepcopy(yaml.safe_load(fh))
    edit(data)
    return TenantSpec.model_validate(data)


# -- baseline: no mutations -------------------------------------------------------------------


def test_baseline_expected_access_matches_the_spec() -> None:
    expected = expected_access(SPEC, [], 0.0)
    assert expected == {
        "p-intern": GENERAL,
        "p-plateng": GENERAL | ENGINEERING | PLATFORM | S1_ARMS,
        "p-finana": GENERAL | FINANCE,
        "p-finlead": GENERAL | FINANCE | LEADERSHIP,
        "p-hr": GENERAL | HR | SPECIAL,
        "p-ceo": BOARD | SPECIAL,
        "p-nest": S2_NESTED,
    }


def test_only_personas_are_evaluated_admin_and_helpers_are_excluded() -> None:
    expected = expected_access(SPEC, [], 0.0)
    assert set(expected) == {u.id for u in SPEC.users if u.persona}
    assert "admin" not in expected and "u-eng1" not in expected


# -- the scenarios in the spec ---------------------------------------------------------------


def test_s1_removing_the_engineering_group_grant() -> None:
    m = mut(
        MutationType.GRANT_REMOVED,
        10.0,
        target="Engineering",
        principal_kind="entra_group",
        principal_ref="grp-engineering",
    )
    before, after = expected_access(SPEC, [m], 9.9), expected_access(SPEC, [m], 10.0)
    assert before["p-plateng"] == GENERAL | ENGINEERING | PLATFORM | S1_ARMS
    assert after["p-plateng"] == GENERAL | PLATFORM | S1_ARMS  # Platform-Secrets has its own grant
    assert after["p-intern"] == before["p-intern"]


def test_s2_removing_a_nested_group_from_its_parent() -> None:
    m = mut(
        MutationType.GROUP_MEMBER_REMOVED,
        10.0,
        group="grp-engineering",
        member_kind="entra_group",
        member_ref="grp-eng-platform",
    )
    after = expected_access(SPEC, [m], 10.0)
    # p-plateng only reached General (via all-staff) and Engineering through the nested group.
    assert after["p-plateng"] == PLATFORM | S1_ARMS
    before = expected_access(SPEC, [m], 9.0)["p-plateng"]
    assert before == GENERAL | ENGINEERING | PLATFORM | S1_ARMS


def test_s4_a_moved_file_keeps_its_own_grants() -> None:
    m = mut(MutationType.FILE_MOVED, 10.0, file="f-direct-share", to_folder="Board")
    before, after = expected_access(SPEC, [m], 9.0), expected_access(SPEC, [m], 10.0)
    assert after == before  # nothing changes for anyone
    assert SPECIAL <= after["p-hr"] and SPECIAL <= after["p-ceo"]
    assert not SPECIAL & after["p-finana"]  # the Board folder's grants are NOT applied to it


def test_a_moved_file_without_own_grants_follows_its_new_folder() -> None:
    spec = spec_with(lambda d: d["special_files"][0].update(inherits=True, grants=[]))
    special = set(generate_canaries(spec)[special_key("f-direct-share")])
    m = mut(MutationType.FILE_MOVED, 10.0, file="f-direct-share", to_folder="Board")
    before, after = expected_access(spec, [m], 9.0), expected_access(spec, [m], 10.0)
    assert special <= before["p-intern"]  # in General: all-staff
    assert not special & after["p-intern"] and special <= after["p-ceo"]  # now in Board


# -- inheritance -------------------------------------------------------------------------------


def test_an_inheriting_subfolder_follows_the_root_and_then_its_parent() -> None:
    def add_child(d: dict[str, Any]) -> None:
        d["folders"].append({"path": "General/Sub", "inherits": True, "doc_count": 1})
        d["documents"]["folder_codes"]["General/Sub"] = "SUB"

    spec = spec_with(add_child)
    sub = set(generate_canaries(spec)["General/Sub"])
    assert sub <= expected_access(spec, [], 0.0)["p-intern"]  # General inherits root grants
    # Break inheritance on General and grant only grp-hr: the child follows General.
    ms = [
        mut(MutationType.INHERITANCE_BROKEN, 1.0, target="General"),
        mut(
            MutationType.GRANT_ADDED,
            2.0,
            target="General",
            principal_kind="entra_group",
            principal_ref="grp-hr",
            role="read",
        ),
    ]
    after = expected_access(spec, ms, 3.0)
    assert sub <= after["p-hr"] and not sub & after["p-intern"]


def test_root_grants_reach_inheriting_items() -> None:
    m = mut(
        MutationType.GRANT_ADDED,
        5.0,
        target="root",
        principal_kind="entra_group",
        principal_ref="grp-board",
        role="read",
    )
    assert GENERAL <= expected_access(SPEC, [m], 5.0)["p-ceo"]
    assert not GENERAL & expected_access(SPEC, [m], 4.0)["p-ceo"]


def test_breaking_inheritance_without_copy_removes_everyone_and_copy_keeps_access() -> None:
    nobody = mut(MutationType.INHERITANCE_BROKEN, 1.0, target="General")
    assert not any(GENERAL & c for c in expected_access(SPEC, [nobody], 2.0).values())
    copied = mut(MutationType.INHERITANCE_BROKEN, 1.0, target="General", copy_role_assignments=True)
    assert expected_access(SPEC, [copied], 2.0) == expected_access(SPEC, [], 2.0)
    restored = [nobody, mut(MutationType.INHERITANCE_RESTORED, 3.0, target="General")]
    assert expected_access(SPEC, restored, 4.0) == expected_access(SPEC, [], 4.0)


# -- time semantics ------------------------------------------------------------------------------


def test_unconfirmed_and_future_mutations_do_not_change_intent() -> None:
    kw: dict[str, Any] = {
        "target": "Engineering",
        "principal_kind": "entra_group",
        "principal_ref": "grp-engineering",
    }
    base = expected_access(SPEC, [], 0.0)
    assert expected_access(SPEC, [mut(MutationType.GRANT_REMOVED, None, **kw)], 1e9) == base
    assert expected_access(SPEC, [mut(MutationType.GRANT_REMOVED, 10.0, **kw)], 9.999) == base
    assert expected_access(SPEC, [mut(MutationType.GRANT_REMOVED, 10.0, **kw)], 10.0) != base


def test_mutations_apply_in_confirmed_order_not_list_order() -> None:
    grant = mut(
        MutationType.GRANT_ADDED,
        5.0,
        target="Board",
        principal_kind="user",
        principal_ref="p-intern",
        role="read",
    )
    remove = mut(
        MutationType.GRANT_REMOVED,
        6.0,
        target="Board",
        principal_kind="user",
        principal_ref="p-intern",
    )
    for order in ([grant, remove], [remove, grant]):
        assert not BOARD & expected_access(SPEC, order, 7.0)["p-intern"]
        assert BOARD <= expected_access(SPEC, order, 5.5)["p-intern"]


def test_timeline_matches_expected_access_at_every_time() -> None:
    ms = [
        mut(MutationType.FILE_MOVED, 3.0, file="f-direct-share", to_folder="Board"),
        mut(
            MutationType.GRANT_REMOVED,
            8.0,
            target="Finance",
            principal_kind="entra_group",
            principal_ref="grp-finance",
        ),
        mut(
            MutationType.GRANT_REMOVED,
            None,
            target="HR",
            principal_kind="entra_group",
            principal_ref="grp-hr",
        ),
    ]
    timeline = IntentTimeline(SPEC, ms)
    assert list(timeline.change_times()) == [3.0, 8.0]
    for t in (-5.0, 0.0, 3.0, 7.99, 8.0, 100.0):
        assert timeline.expected_at(t) == expected_access(SPEC, ms, t)


# -- fail closed ---------------------------------------------------------------------------------


def test_limited_access_and_unknown_roles_are_not_read_access() -> None:
    for role in ("limited_access", "contribute-ish", ""):
        m = mut(
            MutationType.GRANT_ADDED,
            1.0,
            target="Board",
            principal_kind="user",
            principal_ref="p-intern",
            role=role,
        )
        assert not BOARD & expected_access(SPEC, [m], 2.0)["p-intern"]


def test_unknown_principal_kind_denies_and_logs(caplog: pytest.LogCaptureFixture) -> None:
    m = mut(
        MutationType.GRANT_ADDED,
        1.0,
        target="Board",
        principal_kind="everyone_except_external_users",
        principal_ref="x",
        role="read",
    )
    with caplog.at_level(logging.WARNING):
        after = expected_access(SPEC, [m], 2.0)
    assert after == expected_access(SPEC, [], 2.0)
    assert "unknown principal kind" in caplog.text


@pytest.mark.parametrize(
    "bad",
    [
        grant_added("General", "user", "p-hr"),  # General inherits: break inheritance first
        grant_added("Nope", "user", "p-hr"),
        grant_added("special:nope", "user", "p-hr"),
        member_added("grp-nope", "user", "p-hr"),
        member_added("grp-hr", "user", "nobody"),
        member_added("grp-hr", "alien", "p-hr"),
        mut(MutationType.INHERITANCE_BROKEN, target="root"),
        mut(MutationType.INHERITANCE_RESTORED, target="root"),
        mut(MutationType.FILE_MOVED, file="nope", to_folder="Board"),
        mut(MutationType.FILE_MOVED, file="f-direct-share", to_folder="Nope"),
    ],
)
def test_invalid_mutations_raise_instead_of_being_ignored(bad: Mutation) -> None:
    with pytest.raises(OracleError):
        expected_access(SPEC, [bad], 2.0)


def test_mutation_parameters_are_validated() -> None:
    with pytest.raises(ValidationError, match="missing parameters"):
        Mutation(type=MutationType.GRANT_ADDED, params={"target": "root"}, confirmed_at=1.0)
    with pytest.raises(ValidationError, match="unknown parameters"):
        Mutation(
            type=MutationType.FILE_MOVED,
            params={"file": "a", "to_folder": "b", "extra": 1},
            confirmed_at=1.0,
        )


def test_a_membership_cycle_created_by_a_mutation_terminates() -> None:
    m = mut(
        MutationType.GROUP_MEMBER_ADDED,
        1.0,
        group="grp-eng-platform",
        member_kind="entra_group",
        member_ref="grp-all-staff",
    )
    after = expected_access(SPEC, [m], 2.0)
    assert GENERAL <= after["p-plateng"]


# -- the S1 control arms and SharePoint groups ---------------------------------------------------


def _revocation(scenario_id: str) -> list[Mutation]:
    scenario = next(sc for sc in SPEC.scenarios if sc.id == scenario_id)
    return [Mutation(type=m.type, params=m.params, confirmed_at=10.0) for m in scenario.mutations]  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("scenario", "lost"),
    [("S1-entra", S1_ENTRA), ("S1-spgroup", S1_SPGROUP), ("S1-direct", S1_DIRECT)],
)
def test_each_s1_arm_revokes_exactly_its_own_folder_for_p_plateng(
    scenario: str, lost: set[str]
) -> None:
    ms = _revocation(scenario)
    before, after = expected_access(SPEC, ms, 9.9), expected_access(SPEC, ms, 10.0)
    assert before["p-plateng"] - after["p-plateng"] == lost
    assert after["p-plateng"] - before["p-plateng"] == set()
    for persona in before:
        if persona != "p-plateng":
            assert before[persona] == after[persona]
            assert not S1_ARMS & after[persona]  # nobody else ever reads the arms
    # the control folder used by the three scenarios is untouched
    assert PLATFORM <= after["p-plateng"]


def test_the_oracle_gives_the_same_answer_for_every_grant_type() -> None:
    """Lag differs by grant type in SharePoint; intent does not (docs/FINDINGS.md)."""
    sizes = {
        s: len(expected_access(SPEC, _revocation(s), 0.0)["p-plateng"])
        for s in ("S1-entra", "S1-spgroup", "S1-direct")
    }
    assert len(set(sizes.values())) == 1


def test_adding_a_user_to_the_sharepoint_group_gives_the_group_s_access() -> None:
    m = mut(
        MutationType.GROUP_MEMBER_ADDED,
        5.0,
        group="sp-s1-spgroup",
        member_kind="user",
        member_ref="p-intern",
    )
    assert expected_access(SPEC, [m], 4.0)["p-intern"] == GENERAL
    assert expected_access(SPEC, [m], 5.0)["p-intern"] == GENERAL | S1_SPGROUP


def test_a_grant_to_a_sharepoint_group_reaches_its_members() -> None:
    ms = [
        grant_added("Board", "sp_group", "sp-s1-spgroup", at=1.0),
    ]
    assert BOARD <= expected_access(SPEC, ms, 1.0)["p-plateng"]
    assert not BOARD & expected_access(SPEC, ms, 1.0)["p-intern"]


def test_an_entra_group_inside_a_sharepoint_group_is_refused_not_guessed() -> None:
    m = mut(
        MutationType.GROUP_MEMBER_ADDED,
        1.0,
        group="sp-s1-spgroup",
        member_kind="entra_group",
        member_ref="grp-hr",
    )
    with pytest.raises(OracleError, match="not verified"):
        expected_access(SPEC, [m], 2.0)


def test_a_sharepoint_group_kind_is_not_accepted_as_a_member_of_an_entra_group() -> None:
    m = mut(
        MutationType.GROUP_MEMBER_ADDED,
        1.0,
        group="grp-hr",
        member_kind="sp_group",
        member_ref="sp-s1-spgroup",
    )
    with pytest.raises(OracleError):
        expected_access(SPEC, [m], 2.0)


def test_every_scenario_mutation_in_the_spec_is_valid_for_the_oracle() -> None:
    for sc in SPEC.scenarios:
        ms = [Mutation(type=m.type, params=m.params, confirmed_at=1.0) for m in sc.mutations]  # type: ignore[arg-type]
        expected_access(SPEC, ms, 2.0)  # raises OracleError if a mutation cannot be interpreted
