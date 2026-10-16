"""Tests for generator.plan (pure) and generator.retry."""

from pathlib import Path

import pytest

from leaklab.generator import GeneratorError, build_plan, choose_sku
from leaklab.generator.plan import assert_prefixed, render_document
from leaklab.generator.retry import (
    gone_ok,
    is_ensureuser_not_found,
    is_member_add_not_ready,
    retry_call,
)
from leaklab.graph_client import GraphError
from leaklab.sp_rest import SPRestError
from leaklab.spec import generate_canaries, load_spec

SPEC = load_spec(Path(__file__).resolve().parents[1] / "tenant_spec.yaml")
PLAN = build_plan(SPEC)


# -- plan ----------------------------------------------------------------------------------------


def test_everything_generated_carries_the_prefix() -> None:
    prefix = SPEC.naming.prefix
    assert PLAN.container == f"{prefix}lab"
    assert all(u.upn.startswith(prefix) and u.nickname.startswith(prefix) for u in PLAN.users)
    assert all(g.name.startswith(prefix) for g in PLAN.groups)
    assert all(d.name.startswith(prefix) for f in PLAN.folders for d in f.docs)


def test_the_admin_is_not_generated_and_its_grant_is_skipped() -> None:
    assert "admin" not in {u.id for u in PLAN.users}
    assert len(PLAN.users) == 11
    assert [g.ref for g in PLAN.root_grants] == ["grp-all-staff"]
    assert any("admin" in s for s in PLAN.skipped)


def test_folders_are_ordered_parents_first_and_grants_follow_the_spec() -> None:
    paths = [f.path for f in PLAN.folders]
    for path in paths:
        parent = path.rpartition("/")[0]
        assert not parent or paths.index(parent) < paths.index(path)
    board = next(f for f in PLAN.folders if f.path == "Board")
    assert {(g.kind, g.ref, g.role_name) for g in board.grants} == {
        ("entra_group", "grp-board", "Read"),
        ("user", "p-ceo", "Read"),
    }
    assert next(f for f in PLAN.folders if f.path == "General").inherits


def test_documents_use_the_oracles_canaries_exactly_once_each() -> None:
    canaries = generate_canaries(SPEC)
    for folder in PLAN.folders:
        assert [d.canary for d in folder.docs] == canaries[folder.path]
        for doc in folder.docs:
            assert doc.text.count(doc.canary) == 1
    [special] = PLAN.special_files
    assert special.doc.canary == canaries["special:f-direct-share"][0]
    assert special.doc.name == "offer-letter-draft.txt"
    assert {(g.ref, g.role_name) for g in special.grants} == {("p-hr", "Read"), ("p-ceo", "Read")}


def test_the_plan_and_the_texts_are_deterministic() -> None:
    assert build_plan(SPEC) == PLAN
    assert PLAN.folders[0].docs[0].text == build_plan(SPEC).folders[0].docs[0].text


def test_a_body_template_with_unknown_placeholders_is_refused() -> None:
    bad = SPEC.model_copy(
        update={"documents": SPEC.documents.model_copy(update={"body_template": "{nope}"})}
    )
    with pytest.raises(GeneratorError, match="body_template"):
        render_document(bad, "t", "k", 1, "CANARY")


def test_assert_prefixed_refuses_foreign_names() -> None:
    assert_prefixed("ll-lab", "ll-")
    for name in ("General", "Documents", "someone@t.com"):
        with pytest.raises(GeneratorError):
            assert_prefixed(name, "ll-")


def test_choose_sku() -> None:
    sku = {
        "skuId": "a",
        "skuPartNumber": "BASIC",
        "appliesTo": "User",
        "capabilityStatus": "Enabled",
        "prepaidUnits": {"enabled": 5},
        "consumedUnits": 1,
    }
    free = {**sku, "skuId": "b", "skuPartNumber": "FREE", "prepaidUnits": {"enabled": 10000}}
    assert choose_sku([sku], "") == "a"
    assert choose_sku([sku, free], "FREE") == "b"
    with pytest.raises(GeneratorError):  # two candidates and no part number: do not guess
        choose_sku([sku, free], "")
    with pytest.raises(GeneratorError):
        choose_sku([{**sku, "consumedUnits": 5}], "")  # no free seat


# -- retry -----------------------------------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, s: float) -> None:
        self.slept.append(s)
        self.now += s


def not_ready() -> GraphError:
    return GraphError(404, "Request_ResourceNotFound", "x")


def test_retry_call_retries_then_succeeds() -> None:
    clock, calls = Clock(), []

    def op() -> str:
        calls.append(1)
        if len(calls) < 3:
            raise not_ready()
        return "ok"

    result = retry_call(
        op, retryable=is_member_add_not_ready, timeout_s=60, sleep=clock.sleep, clock=clock
    )
    assert result == "ok" and len(calls) == 3 and clock.slept == [5.0, 5.0]


def test_retry_call_gives_up_at_the_timeout_and_raises_the_last_error() -> None:
    clock = Clock()

    def op() -> None:
        raise not_ready()

    with pytest.raises(GraphError):
        retry_call(
            op, retryable=is_member_add_not_ready, timeout_s=12, sleep=clock.sleep, clock=clock
        )
    assert clock.now <= 12


def test_retry_call_does_not_retry_other_errors() -> None:
    clock = Clock()

    def op() -> None:
        raise GraphError(403, "Forbidden", "no")

    with pytest.raises(GraphError):
        retry_call(
            op, retryable=is_member_add_not_ready, timeout_s=60, sleep=clock.sleep, clock=clock
        )
    assert clock.slept == []


def test_ensureuser_predicate_matches_only_the_verified_error() -> None:
    nf = SPRestError(400, "/x", {"odata.error": {"message": {"value": "user could not be found."}}})
    assert is_ensureuser_not_found(nf)
    assert not is_ensureuser_not_found(SPRestError(400, "/x", {"error": "other"}))
    assert not is_ensureuser_not_found(SPRestError(500, "/x", {"error": "could not be found"}))
    assert not is_ensureuser_not_found(ValueError("could not be found"))


def test_gone_ok_treats_404_as_already_gone() -> None:
    def gone() -> None:
        raise GraphError(404, "itemNotFound", "")

    def forbidden() -> None:
        raise GraphError(403, "Forbidden", "")

    assert gone_ok(lambda: None) is True
    assert gone_ok(gone) is False
    with pytest.raises(GraphError):
        gone_ok(forbidden)
