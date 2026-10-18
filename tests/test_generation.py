"""Tenant generations: the id comes from the live tenant, and canaries never collide across them."""

import re

import pytest

from generator_fakes import FakeClock, FakeTenant
from leaklab.generator import generate, teardown, verify
from leaklab.runner import build_report
from leaklab.runner.run import _check_resume
from leaklab.spec import generate_canaries, generation_from_epoch
from runner_fakes import SPEC, make_env

T1, T2 = 1_792_312_709.0, 1_792_400_000.0


def built(tenant: FakeTenant, now: float):  # type: ignore[no-untyped-def]
    tenant.now = now
    env = make_env(tenant, FakeClock())
    generate(env)
    return env


def doc_texts(env) -> list[str]:  # type: ignore[no-untyped-def]
    return [d.text for f in env.plan.folders for d in f.docs]


def test_generate_adopts_the_generation_of_the_new_container() -> None:
    env = built(FakeTenant(), T1)
    assert env.generation == generation_from_epoch(T1) and env.generation_found
    assert env.spec.naming.generation == env.generation
    canaries = {c for cs in generate_canaries(env.spec).values() for c in cs}
    assert all(c.startswith(f"CANARY-{env.generation}-") for c in canaries)
    assert all(re.search(r"CANARY-G[0-9A-Z]{6}-", t) for t in doc_texts(env))
    # every document of the plan carries a canary of THIS generation in its text
    for folder in env.plan.folders:
        for doc in folder.docs:
            assert doc.canary in doc.text and doc.canary.startswith(f"CANARY-{env.generation}-")


def test_a_fresh_process_finds_the_same_generation_without_generating() -> None:
    tenant = FakeTenant()
    first = built(tenant, T1)
    second = make_env(tenant, FakeClock())
    assert second.generation == "G000000" and not second.generation_found  # the template
    assert second.discover_generation() == first.generation
    assert second.plan.folders[0].docs[0].canary == first.plan.folders[0].docs[0].canary
    assert verify(second).ok


def test_without_a_tenant_the_template_generation_applies() -> None:
    env = make_env(FakeTenant(), FakeClock())
    assert env.discover_generation() == "G000000" and not env.generation_found


def test_teardown_and_generate_again_is_a_new_generation_with_new_canaries() -> None:
    tenant = FakeTenant()
    first = built(tenant, T1)
    old = {c for cs in generate_canaries(first.spec).values() for c in cs}
    teardown(first)
    second = built(tenant, T2)
    new = {c for cs in generate_canaries(second.spec).values() for c in cs}
    assert second.generation == generation_from_epoch(T2) != first.generation
    assert old.isdisjoint(new) and len(new) == len(old) == 45
    assert verify(second).ok


def test_the_oracle_follows_the_generation_of_the_spec() -> None:
    from leaklab.oracle import expected_access

    env = built(FakeTenant(), T1)
    expected = expected_access(env.spec, [], 0.0)
    own = {c for cs in generate_canaries(env.spec).values() for c in cs}
    assert all(c in own for canaries in expected.values() for c in canaries)
    template = {c for cs in generate_canaries(SPEC).values() for c in cs}
    assert not any(c in template for canaries in expected.values() for c in canaries)


def test_a_journal_is_scored_with_the_generation_it_names() -> None:
    header = {
        "type": "run_started",
        "run_id": "x",
        "scenarios": [],
        "systems": ["fake"],
        "config": {"n_consecutive": 3, "cap_s": 60.0},
        "generation": "GABC123",
    }
    report = build_report(SPEC, [header])
    assert report.run_id == "x" and report.results == []


def test_resume_refuses_a_journal_of_another_generation() -> None:
    header = {
        "type": "run_started",
        "scenarios": ["S1-direct"],
        "systems": ["fake"],
        "spec_sha256": "s",
        "generation": "GAAAAAA",
    }
    _check_resume([header], ["S1-direct"], ["fake"], "s", "GAAAAAA")
    with pytest.raises(Exception, match="regenerated"):
        _check_resume([header], ["S1-direct"], ["fake"], "s", "GBBBBBB")


def test_the_template_never_leaks_into_a_generated_tenant() -> None:
    env = built(FakeTenant(), T1)
    assert "G000000" not in "".join(doc_texts(env))
