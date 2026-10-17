"""The state store against the oracle: after ANY sequence of mutations, who the store says may read
which document equals who the oracle says should.

The oracle (leaklab.oracle) computes expected access from tenant_spec.yaml and the mutation list only.
The store never sees either: it reads the (fake) tenant through the same delta and REST calls the real
pollers use. If they agree after random sequences of grant changes, group-membership changes (users and
nested groups, Entra and SharePoint groups) and file moves, the store's picture of the tenant is right.
The oracle is imported HERE only: tests/test_setup_b_isolation.py checks src/ never imports it.
"""

from collections.abc import Callable
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from leaklab.oracle import expected_access
from leaklab.runner.scenarios import scenario_mutations
from leaklab.runner.tenant import resolve_items
from leaklab.setup_b.state.repository import StateRepository
from leaklab.spec import generate_canaries
from runner_fakes import SPEC
from setup_b_world import World

Factory = Callable[[], StateRepository]
CANARIES = generate_canaries(SPEC)
ALL = [c for cs in CANARIES.values() for c in cs]
PERSONAS = [u.id for u in SPEC.users if u.persona]
UNIQUE_TARGETS = [f.path for f in SPEC.folders if not f.inherits] + ["special:f-direct-share"]
GROUPS = [g.id for g in SPEC.groups]  # parents come before the groups they contain: no cycles below
FOLDERS = [f.path for f in SPEC.folders]
PRINCIPALS = (
    [("user", p) for p in PERSONAS]
    + [("entra_group", g) for g in GROUPS]
    + [("sp_group", "sp-s1-spgroup")]
)


def mutation_strategy() -> st.SearchStrategy[tuple[str, dict[str, Any]]]:
    grant = st.tuples(st.sampled_from(UNIQUE_TARGETS), st.sampled_from(PRINCIPALS))
    nesting = st.tuples(st.integers(0, len(GROUPS) - 2), st.integers(1, len(GROUPS) - 1)).filter(
        lambda ij: ij[0] < ij[1]
    )  # a later group may go inside an earlier one: acyclic
    return st.one_of(
        grant.map(
            lambda tp: (
                "grant_added",
                {
                    "target": tp[0],
                    "principal_kind": tp[1][0],
                    "principal_ref": tp[1][1],
                    "role": "read",
                },
            )
        ),
        grant.map(
            lambda tp: (
                "grant_removed",
                {"target": tp[0], "principal_kind": tp[1][0], "principal_ref": tp[1][1]},
            )
        ),
        st.tuples(
            st.sampled_from(["group_member_added", "group_member_removed"]),
            st.sampled_from(PERSONAS),
            st.sampled_from(GROUPS),
        ).map(  # noqa: E501
            lambda t: (t[0], {"group": t[2], "member_kind": "user", "member_ref": t[1]})
        ),
        st.tuples(
            st.sampled_from(["group_member_added", "group_member_removed"]),
            st.sampled_from(PERSONAS),
        ).map(  # noqa: E501
            lambda t: (t[0], {"group": "sp-s1-spgroup", "member_kind": "user", "member_ref": t[1]})
        ),
        st.tuples(st.sampled_from(["group_member_added", "group_member_removed"]), nesting).map(
            lambda t: (
                t[0],
                {
                    "group": GROUPS[t[1][0]],
                    "member_kind": "entra_group",
                    "member_ref": GROUPS[t[1][1]],
                },
            )
        ),
        st.sampled_from(FOLDERS).map(
            lambda f: ("file_moved", {"file": "f-direct-share", "to_folder": f})
        ),
    )


def assert_store_equals_oracle(w: World, doc_ids: dict[str, str]) -> None:
    expected = expected_access(SPEC, w.env.log.oracle_mutations(), float("inf"))
    by_doc = {d: c for c, d in doc_ids.items()}
    for persona in PERSONAS:
        readable = w.reader.readable(w.uid(persona))
        got = {by_doc[d] for d in readable if d in by_doc}
        assert got == expected[persona], (
            f"{persona}: store-only {sorted(got - expected[persona])}, "
            f"oracle-only {sorted(expected[persona] - got)}"
        )


def run_sequence(repo: StateRepository, sequence: list[tuple[str, dict[str, Any]]]) -> None:
    w = World(repo)
    doc_ids = {c: str(i["id"]) for c, i in resolve_items(w.env, ALL).items()}
    w.sync()
    assert_store_equals_oracle(w, doc_ids)  # the generated tenant itself
    for kind, params in sequence:
        w.apply(kind, **params)
        w.sync()
    assert_store_equals_oracle(w, doc_ids)
    events = len(w.repo.events())
    assert w.sync()[0].changes == 0 and w.sync()[1].events == 0  # nothing left to find
    assert len(w.repo.events()) == events
    drift = w.acl.verify_all()  # the safety net must have nothing to correct
    assert drift.events == 0, [
        e for e in w.repo.events("acl") if e.detail.get("found_by") == "verify"
    ]
    assert all(e.seen_at <= e.applied_at for e in w.repo.events())


@settings(
    max_examples=25,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(sequence=st.lists(mutation_strategy(), min_size=1, max_size=6))
def test_after_any_sequence_of_mutations_the_store_equals_the_oracle(
    make_repo: Factory, sequence: list[tuple[str, dict[str, Any]]]
) -> None:
    run_sequence(make_repo(), sequence)


@pytest.mark.parametrize(
    "scenario",
    [
        "S1-parent-revocation",
        "S2-nested-group-removal",
        "S4-move-into-restricted",
        "S1-entra",
        "S1-spgroup",
        "S1-direct",
    ],
)
def test_each_scenario_of_the_spec_ends_with_the_store_equal_to_the_oracle(
    make_repo: Factory, scenario: str
) -> None:
    mutations = scenario_mutations(next(s for s in SPEC.scenarios if s.id == scenario))
    run_sequence(make_repo(), [(m.type.value, dict(m.params)) for m in mutations])


def test_a_long_fixed_sequence_mixing_every_kind_of_change(make_repo: Factory) -> None:
    run_sequence(
        make_repo(),
        [
            (
                "grant_removed",
                {"target": "Board", "principal_kind": "user", "principal_ref": "p-ceo"},
            ),
            (
                "group_member_removed",
                {
                    "group": "grp-engineering",
                    "member_kind": "entra_group",
                    "member_ref": "grp-eng-platform",
                },
            ),  # noqa: E501
            (
                "grant_added",
                {
                    "target": "Finance",
                    "principal_kind": "sp_group",
                    "principal_ref": "sp-s1-spgroup",
                    "role": "read",
                },
            ),  # noqa: E501
            (
                "group_member_added",
                {"group": "sp-s1-spgroup", "member_kind": "user", "member_ref": "p-intern"},
            ),  # noqa: E501
            ("file_moved", {"file": "f-direct-share", "to_folder": "Finance"}),
            (
                "group_member_added",
                {"group": "grp-hr", "member_kind": "user", "member_ref": "p-intern"},
            ),  # noqa: E501
            (
                "grant_added",
                {
                    "target": "S1-Direct",
                    "principal_kind": "entra_group",
                    "principal_ref": "grp-board",
                    "role": "read",
                },
            ),  # noqa: E501
            (
                "group_member_removed",
                {"group": "grp-all-staff", "member_kind": "entra_group", "member_ref": "grp-hr"},
            ),  # noqa: E501
        ],
    )
