"""Which pairs the runner watches for each scenario, derived from the spec and the oracle only."""

import copy
from typing import Any

import pytest
import yaml

from leaklab.oracle import Mutation, MutationType
from leaklab.runner import RunnerError, derive_plan, group_scenarios
from leaklab.runner.scenarios import plan_from_dict, plan_to_dict
from leaklab.spec import TenantSpec, generate_canaries
from runner_fakes import SPEC, SPEC_PATH

CAN = generate_canaries(SPEC)
PLAT = CAN["Engineering/Platform-Secrets"][0]
OTHERS = {"p-ceo", "p-finana", "p-finlead", "p-hr", "p-intern"}


@pytest.mark.parametrize(
    ("scenario", "folder"),
    [("S1-direct", "S1-Direct"), ("S1-spgroup", "S1-SPGroup"), ("S1-entra", "S1-Entra")],
)
def test_each_s1_arm_revokes_its_two_canaries_for_p_plateng_only(
    scenario: str, folder: str
) -> None:
    plan = derive_plan(SPEC, scenario)
    assert plan.revoking == {("p-plateng", c) for c in CAN[folder]}
    assert plan.granting == frozenset()
    # one canary of the touched folder, read by everybody else, to catch a leak caused by the change
    expected_targeted = {(p, CAN[folder][0]) for p in OTHERS} if scenario == "S1-direct" else set()
    assert plan.targeted == expected_targeted
    assert plan.control == ("p-plateng", PLAT)  # the spec's control folder
    assert plan.pairs == plan.flipped | plan.targeted | {plan.control}


def test_s1_parent_revocation_flips_the_engineering_documents() -> None:
    plan = derive_plan(SPEC, "S1-parent-revocation")
    assert plan.revoking == {("p-plateng", c) for c in CAN["Engineering"]}
    assert plan.control is not None and plan.control not in plan.flipped | plan.targeted
    persona, canary = plan.control
    assert persona == "p-plateng"  # the persona that is revoked is the one whose control matters
    assert canary in CAN["General"] or canary in CAN["Engineering/Platform-Secrets"]


def test_s2_flips_what_p_plateng_reached_through_the_nested_group() -> None:
    plan = derive_plan(SPEC, "S2-nested-group-removal")
    assert plan.revoking == {("p-plateng", c) for c in CAN["General"] + CAN["Engineering"]}


def test_s4_flips_nothing_but_watches_the_moved_file_for_every_persona() -> None:
    plan = derive_plan(SPEC, "S4-move-into-restricted")
    special = CAN["special:f-direct-share"][0]
    assert plan.flipped == frozenset()
    assert plan.targeted == {(p, special) for p in OTHERS | {"p-plateng"}}
    assert plan.control is not None and plan.control[1] != special


def test_a_scenario_without_mutations_is_refused() -> None:
    with pytest.raises(RunnerError, match="no mutations"):
        derive_plan(SPEC, "S6-cached-answer")


def test_an_unknown_scenario_is_refused_and_the_known_ones_are_listed() -> None:
    with pytest.raises(RunnerError, match="S1-direct"):
        derive_plan(SPEC, "S99")


def test_prior_mutations_change_what_a_later_scenario_flips() -> None:
    s1 = derive_plan(SPEC, "S1-parent-revocation")
    applied = [m.model_copy(update={"confirmed_at": 5.0}) for m in s1.mutations]
    after = derive_plan(SPEC, "S2-nested-group-removal", applied)
    assert after.revoking == {("p-plateng", c) for c in CAN["General"]}  # Engineering already gone


def test_a_control_nobody_keeps_is_an_error() -> None:
    with open(SPEC_PATH, encoding="utf-8") as fh:
        data: dict[str, Any] = copy.deepcopy(yaml.safe_load(fh))
    next(s for s in data["scenarios"] if s["id"] == "S1-direct")["control"] = "S1-Direct"
    spec = TenantSpec.model_validate(data)
    with pytest.raises(RunnerError, match="stays allowed"):
        derive_plan(spec, "S1-direct")


def test_disjoint_scenarios_share_a_group_and_overlapping_ones_do_not() -> None:
    arms = [derive_plan(SPEC, s) for s in ("S1-direct", "S1-spgroup", "S1-entra")]
    assert [[p.id for p in g] for g in group_scenarios(arms)] == [
        ["S1-direct", "S1-spgroup", "S1-entra"]
    ]
    clash = [derive_plan(SPEC, s) for s in ("S1-parent-revocation", "S2-nested-group-removal")]
    assert [[p.id for p in g] for g in group_scenarios(clash)] == [
        ["S1-parent-revocation"],
        ["S2-nested-group-removal"],
    ]
    # S1-parent only touches Engineering: it does not collide with the S1 arms, so all share a loop
    mixed = [derive_plan(SPEC, s) for s in ("S1-direct", "S1-parent-revocation", "S1-entra")]
    assert len(group_scenarios(mixed)) == 1


def test_a_scenario_that_touches_anothers_control_cannot_share_its_loop() -> None:
    # S1-parent flips Engineering; a scenario whose control is an Engineering canary must wait
    s1 = derive_plan(SPEC, "S1-parent-revocation")
    eng = CAN["Engineering"][0]
    other = derive_plan(SPEC, "S1-direct")
    clashing = type(other)(
        id="x",
        mutations=other.mutations,
        revoking=other.revoking,
        granting=other.granting,
        targeted=other.targeted,
        control=("p-plateng", eng),
    )
    assert len(group_scenarios([s1, clashing])) == 2


def test_the_plan_survives_a_round_trip_through_the_journal_form() -> None:
    plan = derive_plan(SPEC, "S1-direct")
    again = plan_from_dict(SPEC, plan_to_dict(plan))
    assert again == plan
    assert isinstance(again.mutations[0], Mutation)
    assert again.mutations[0].type is MutationType.GRANT_REMOVED


def test_an_automatic_control_is_never_a_pair_the_scenario_touches() -> None:
    """With no `control` in the spec, the first stable pair is taken - but never a touched one."""
    with open(SPEC_PATH, encoding="utf-8") as fh:
        data: dict[str, Any] = copy.deepcopy(yaml.safe_load(fh))
    data["scenarios"].append(
        {
            "id": "X-noop",
            "description": "Re-grant what p-ceo already has on Board (test only).",
            "mutations": [
                {
                    "type": "grant_added",
                    "params": {
                        "target": "Board",
                        "principal_kind": "user",
                        "principal_ref": "p-ceo",
                        "role": "read",
                    },
                }
            ],
        }
    )
    plan = derive_plan(TenantSpec.model_validate(data), "X-noop")
    assert plan.flipped == frozenset()
    assert plan.targeted  # Board's first canary, for every persona
    assert plan.control is not None and plan.control not in plan.targeted
    assert plan.control[0] == "p-ceo"
