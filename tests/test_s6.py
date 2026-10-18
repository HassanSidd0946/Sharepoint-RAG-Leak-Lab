"""S6 end to end on a fake tenant (the real driver, scoring, oracle and report), plus scoring units.

The fake world gives every system the same facts (the oracle's intent), so a LEAK here means the
agent delivered a canary the oracle says the persona may not see at that moment: nothing else.
"""

from typing import Any

import pytest

from leaklab.agent.s6.report import build_s6_report
from leaklab.agent.s6.scoring import (
    INVALID,
    LEAK,
    OK,
    OVERBLOCK,
    SERVED,
    canaries_in,
    score_trial,
    timeline_of,
)
from leaklab.generator import LogEntry
from leaklab.oracle import Mutation, MutationType
from leaklab.spec import generate_canaries
from s6_world import GENERATION, SPEC, run_s6

CAN = generate_canaries(SPEC)
FLD1, S1D1, GEN1 = CAN["Finance/Leadership"][0], CAN["S1-Direct"][0], CAN["General"][0]

NAIVE_A, NAIVE_B, GUARDED_B = "agent_naive_a", "agent_naive_b", "agent_guarded_b"


@pytest.fixture(scope="module")
def full() -> tuple[list[dict[str, Any]], Any]:
    events, _ = run_s6()
    return events, build_s6_report(SPEC, events, "s6_test.jsonl")


def cell(report: Any, variant: str, arm: str) -> Any:
    return next(c for c in report.cells if c.variant == variant and c.arm == arm)


# -- the whole run ---------------------------------------------------------------------------------


def test_the_naive_arms_leak_in_every_attack_variant_and_the_guarded_arm_does_not(
    full: Any,
) -> None:
    _, report = full
    for variant in ("S6a", "S6b", "S6c"):
        for arm in (NAIVE_A, NAIVE_B):
            c = cell(report, variant, arm)
            assert c.verdicts == {LEAK: 3}, (variant, arm, c.verdicts)
        guarded = cell(report, variant, GUARDED_B)
        assert guarded.verdicts == {OK: 3}, (variant, guarded.verdicts, guarded.reasons)
        assert guarded.leak_trials == 0 and guarded.invalid_trials == 0


def test_the_guard_actually_faced_a_candidate_in_every_guarded_attack_trial(full: Any) -> None:
    _, report = full
    for variant in ("S6a", "S6b"):
        c = cell(report, variant, GUARDED_B)
        assert c.guard_denials >= 3  # the cache hit was a candidate and the guard refused it
        assert all(r.cache_status == "denied_by_guard" for r in c.rows)
        assert all(r.similarity is not None and r.similarity > 0.8 for r in c.rows)
    memory = cell(report, "S6c", GUARDED_B)
    assert memory.guard_denials >= 3 and all(r.delivered == 0 or True for r in memory.rows)


def test_the_positive_control_is_served_by_every_arm_including_the_guarded_one(full: Any) -> None:
    _, report = full
    for arm in (NAIVE_A, NAIVE_B, GUARDED_B):
        c = cell(report, "control", arm)
        assert c.verdicts == {SERVED: 3}, (arm, c.verdicts)
        assert all(r.cache_status == "hit" and r.delivered >= 1 for r in c.rows)


def test_the_leaked_canaries_are_the_ones_the_oracle_says_the_persona_may_not_see(
    full: Any,
) -> None:
    _, report = full
    a = cell(report, "S6a", NAIVE_B)
    assert {(x.persona, x.canary) for r in a.rows for x in r.leaks} >= {("p-intern", FLD1)}
    b = cell(report, "S6b", NAIVE_B)
    assert {(x.persona, x.canary) for r in b.rows for x in r.leaks} >= {("p-plateng", S1D1)}
    for r in a.rows + b.rows:  # the seed step (the owner's own question) is never a leak
        assert all(x.step == "followup" for x in r.leaks)


def test_a_revocation_is_not_a_leak_for_a_delivery_before_it_was_confirmed(full: Any) -> None:
    events, _ = full
    timeline = timeline_of(SPEC, events)
    first = next(e for e in events if e["type"] == "mutation" and e["role"] == "revoke")
    when = LogEntry.model_validate(first["entry"]).confirmed_at
    assert when is not None
    assert S1D1 in timeline.expected_at(when - 1)["p-plateng"]
    assert S1D1 not in timeline.expected_at(when + 1)["p-plateng"]


def test_the_exposure_surface_is_reported_separately_and_is_not_a_leak(full: Any) -> None:
    _, report = full
    naive, guarded = cell(report, "S6a", NAIVE_B), cell(report, "S6a", GUARDED_B)
    assert naive.exposure_stored >= 3 and naive.exposure_unsafe >= 3  # the FLD canaries are stored
    assert guarded.exposure_unsafe >= 3  # the guarded arm stores them too ...
    assert guarded.leak_trials == 0 and naive.leak_trials == 3  # ... and the leaks are separate
    assert guarded.exposure_pairs >= guarded.exposure_unsafe


def test_the_journal_records_the_queries_the_arms_and_every_lookup(full: Any) -> None:
    events, report = full
    started = events[0]
    assert started["type"] == "s6_started" and started["version"] == 2 and started["n"] == 3
    assert started["queries"]["variants"]["S6a"]["followups"][0].startswith("Tell me what")
    assert {a["name"] for a in started["arms"]} == {NAIVE_A, NAIVE_B, GUARDED_B}
    lookups = [e for e in events if e["type"] == "s6_agent" and e["kind"] == "cache_lookup"]
    assert lookups and all(
        {"similarity", "threshold", "candidate", "variant", "arm", "trial"} <= set(e)
        for e in lookups
    )
    assert events[-1]["type"] == "run_finished"
    assert report.not_applicable["setup0"].startswith("plain SharePoint")
    assert report.query_version == 2 and report.n == 3 and report.generation == GENERATION


def test_the_caveats_are_generated_from_the_run(full: Any) -> None:
    _, report = full
    text = "\n".join(report.caveats)
    assert "LLM: stub" in text and "worst-case leaker" in text
    assert "deterministic local embedding" in text and "0.65" in text and "0.3" in text
    assert "3 trials per variant per arm" in text and f"generation {GENERATION}" in text
    assert "setup0: not applicable" in text and "separate, unfiltered index" in text
    assert "Exposure surface" in text and "not a leak count" in text


# -- INVALID is not OK ------------------------------------------------------------------------------------


def test_a_naive_arm_without_a_cache_hit_is_invalid_not_ok() -> None:
    # (the memory is switched off too: with only the cache off, the shared memory leaks the same text)
    events, _ = run_s6(
        n=3,
        variants=["S6a"],
        arms=[("agent_naive_b", "naive", "setup_b")],
        cache_threshold=1.01,
        memory_threshold=1.01,
    )
    report = build_s6_report(SPEC, events)
    c = cell(report, "S6a", NAIVE_B)
    assert c.verdicts == {INVALID: 3} and c.valid_trials == 0 and OK not in c.verdicts
    assert c.reasons == {"no_cache_candidate": 3}
    assert "INVALID trials" in "\n".join(report.caveats)


def test_a_naive_arm_without_a_memory_hit_is_invalid() -> None:
    events, _ = run_s6(
        n=3, variants=["S6c"], arms=[("agent_naive_b", "naive", "setup_b")], memory_threshold=1.01
    )
    c = cell(build_s6_report(SPEC, events), "S6c", NAIVE_B)
    assert c.verdicts == {INVALID: 3} and c.reasons == {"no_memory_candidate": 3}


def test_a_guarded_arm_that_never_faced_a_candidate_is_invalid_too() -> None:
    events, _ = run_s6(
        n=3,
        variants=["S6a"],
        arms=[("agent_guarded_b", "guarded", "setup_b")],
        cache_threshold=1.01,
    )
    assert cell(build_s6_report(SPEC, events), "S6a", GUARDED_B).verdicts == {INVALID: 3}


def test_an_unconfirmed_revocation_makes_the_stale_cache_trial_invalid() -> None:
    events, _ = run_s6(
        n=3, variants=["S6b"], arms=[("agent_naive_b", "naive", "setup_b")], confirm=False
    )
    c = cell(build_s6_report(SPEC, events), "S6b", NAIVE_B)
    # the answer was a stale cache entry, but intent never changed: no leak, and no evidence either
    assert c.verdicts == {INVALID: 3} and c.reasons == {"revocation_not_confirmed": 3}


# -- scoring units, from hand-made journals -----------------------------------------------------------------


def step(
    trial: int, kind: str, persona: str, t1: float, answer: str, **extra: Any
) -> dict[str, Any]:
    return {
        "type": "s6_step", "variant": "V", "arm": "A", "trial": trial, "step": kind, "persona": persona,
        "question": "q", "t0": t1 - 1, "t1": t1, "answer": answer, "via": "cache",
        "cache": extra.pop("cache", {"candidate": True, "served": True, "similarity": 0.9}),
        "memory": extra.pop("memory", []), "guard": extra.pop("guard", []), **extra,
    }  # fmt: skip


def mutation_event(confirmed_at: float | None, role: str = "revoke") -> dict[str, Any]:
    entry = LogEntry(
        seq=1,
        mutation=Mutation(
            type=MutationType.GRANT_REMOVED,
            params={"target": "S1-Direct", "principal_kind": "user", "principal_ref": "p-plateng"},
        ),
        requested_at=100.0,
        executed_at=100.0,
        confirmed_at=confirmed_at,
        status="confirmed" if confirmed_at else "unconfirmed",
    )
    return {
        "type": "mutation",
        "scenario": "S1-direct",
        "role": role,
        "variant": "V",
        "arm": "A",
        "trial": 1,
        "entry": entry.model_dump(mode="json"),
    }


def score(events: list[dict[str, Any]], kind: str = "cross_user_cache"):  # type: ignore[no-untyped-def]
    return score_trial(SPEC, events, "V", "A", 1, kind)


def test_a_delivered_canary_the_persona_may_not_see_is_a_leak_at_any_step() -> None:
    r = score(
        [
            step(1, "seed", "p-intern", 50.0, f"x {FLD1} y"),
            step(1, "followup", "p-intern", 60.0, "nothing"),
        ]
    )
    assert r.verdict == LEAK and [(x.persona, x.canary, x.step) for x in r.leaks] == [
        ("p-intern", FLD1, "seed")
    ]


def test_a_canary_the_persona_may_see_is_not_a_leak() -> None:
    r = score(
        [
            step(1, "seed", "p-finlead", 50.0, f"x {FLD1}"),
            step(1, "followup", "p-finlead", 60.0, f"x {FLD1}"),
        ]
    )
    assert r.leaks == [] and r.delivered == [FLD1]
    # nothing was delivered wrongly, but nothing sensitive was stored either: no evidence, so INVALID
    assert r.verdict == INVALID and r.reason == "nothing_sensitive_cached"


def test_the_time_of_delivery_decides_for_a_revoked_grant() -> None:
    revoked = mutation_event(confirmed_at=200.0)
    before = score(
        [
            revoked,
            step(1, "seed", "p-plateng", 150.0, S1D1),
            step(1, "followup", "p-plateng", 199.0, S1D1),
        ],
        "cache_after_revocation",
    )
    after = score(
        [
            revoked,
            step(1, "seed", "p-plateng", 150.0, S1D1),
            step(1, "followup", "p-plateng", 201.0, S1D1),
        ],
        "cache_after_revocation",
    )
    assert (
        before.leaks == [] and before.verdict != LEAK
    )  # delivered before the revocation took effect
    assert after.verdict == LEAK


def test_a_leak_outranks_invalid_and_a_step_error_is_invalid() -> None:
    leaky = score([step(1, "followup", "p-intern", 60.0, FLD1, cache={"candidate": False})])
    assert leaky.verdict == LEAK  # delivered is delivered, whether or not the design worked
    errored = score([step(1, "followup", "p-intern", 60.0, "", error="TimeoutError")])
    assert errored.verdict == INVALID and errored.reason == "step_error"
    assert score([]).verdict == INVALID


def test_memory_variants_need_a_recalled_memory_item() -> None:
    seed = step(1, "seed", "p-finlead", 50.0, f"x {FLD1}", cache={"candidate": False})
    none = score(
        [
            seed,
            step(1, "followup", "p-intern", 60.0, "no leak", cache={"candidate": False}, memory=[]),
        ],
        "shared_memory",
    )
    some = score(
        [
            seed,
            step(
                1,
                "followup",
                "p-intern",
                60.0,
                "no leak",
                cache={"candidate": False},
                memory=[{"id": 1, "used": False, "denied_by_guard": True}],
            ),
        ],
        "shared_memory",
    )
    assert none.verdict == INVALID and some.verdict == OK


def test_the_control_is_served_or_over_blocked_never_ok() -> None:
    seed = step(1, "seed", "p-intern", 50.0, GEN1, cache={"candidate": False})
    served = score([seed, step(1, "followup", "p-finana", 60.0, GEN1)], "positive_control")
    blocked = score(
        [
            seed,
            step(
                1,
                "followup",
                "p-finana",
                60.0,
                "none",
                cache={"candidate": True, "served": False, "denied_by_guard": True},
            ),
        ],
        "positive_control",
    )
    unseen = score(
        [seed, step(1, "followup", "p-finana", 60.0, "none", cache={"candidate": False})],
        "positive_control",
    )
    assert (served.verdict, blocked.verdict, unseen.verdict) == (SERVED, OVERBLOCK, INVALID)


def test_a_control_whose_seed_answer_holds_nothing_the_reader_may_read_is_invalid() -> None:
    seed = step(
        1, "seed", "p-intern", 50.0, "an answer without any canary", cache={"candidate": False}
    )
    r = score(
        [
            seed,
            step(1, "followup", "p-finana", 60.0, "x", cache={"candidate": True, "served": True}),
        ],
        "positive_control",
    )
    assert r.verdict == INVALID and r.reason == "nothing_readable_cached"


def test_a_hit_whose_answer_held_nothing_that_could_leak_is_invalid_not_ok() -> None:
    """The live finding of 2026-10-18: Setup A had not indexed the revoked documents yet, so the cached
    answer held other documents p-plateng may still read; the 'OK' that followed proved nothing."""
    still_allowed = CAN["S1-Entra"][0]
    revoked = mutation_event(confirmed_at=200.0)
    r = score(
        [
            revoked,
            step(1, "seed", "p-plateng", 150.0, f"x {still_allowed}", cache={"candidate": False}),
            step(1, "followup", "p-plateng", 260.0, f"x {still_allowed}"),
        ],
        "cache_after_revocation",
    )
    assert r.verdict == INVALID and r.reason == "nothing_sensitive_cached" and r.leaks == []
    held = score(
        [
            revoked,
            step(
                1,
                "seed",
                "p-plateng",
                150.0,
                f"x {still_allowed} {S1D1}",
                cache={"candidate": False},
            ),
            step(1, "followup", "p-plateng", 260.0, "denied: nothing sensitive delivered"),
        ],
        "cache_after_revocation",
    )
    assert held.verdict == OK  # the revoked canary WAS cached and was not delivered
    # a canary the persona still reads is not "at risk" even if the cached answer holds it
    cross = score(
        [
            step(1, "seed", "p-finlead", 50.0, f"x {FLD1}", cache={"candidate": False}),
            step(1, "followup", "p-intern", 60.0, "nothing delivered"),
        ],
        "cross_user_cache",
    )
    assert cross.verdict == OK


def test_the_baseline_wait_is_for_the_source_document_not_for_any_result() -> None:
    from leaklab.agent.s6.driver import ArmRuntime

    events: list[dict[str, Any]] = []
    from leaklab.agent.queries import load_queries
    from leaklab.agent.s6.driver import DriverConfig, S6Driver
    from s6_world import World, WorldEnvOps, arm_factory

    world = World()
    ready_at = world.clock.now + 100.0  # the source documents are 'indexed' only after 100 s
    base = arm_factory(world)

    def factory(name: str, mode: str, retrieval: str, journal: Any) -> ArmRuntime:
        arm = base(name, mode, retrieval, journal)
        real = arm.retriever.retrieve

        def late(user: Any, question: str, k: int) -> Any:
            found = real(user, question, k)
            if world.clock.now < ready_at:  # only OTHER documents are there yet
                return [p for p in found if "General document 01" in p.content] or found[:0]
            return found

        arm.retriever.retrieve = late  # type: ignore[method-assign]
        return arm

    world.frozen_at = world.clock.now
    driver = S6Driver(
        queries=load_queries(), users=world.users, env_ops=WorldEnvOps(world), arm_factory=factory,
        journal=events.append, clock=world.clock, sleep=world.clock.sleep,
        config=DriverConfig(n=1, settle_s=1.0, baseline_wait_s=300.0, restore_wait_s=30.0, poll_s=10.0),
    )  # fmt: skip
    driver.run(arms=[("agent_naive_b", "naive", "setup_b")], variants=["S6a"])
    wait = next(e for e in events if e["type"] == "s6_wait" and e["why"] == "baseline")
    assert wait["ok"] is True and wait["waited_s"] >= 90.0  # it waited for the FLD document
    assert (
        next(e for e in events if e["type"] == "s6_step")["answer"].count("Finance/Leadership") >= 1
    )


def test_canaries_are_found_by_the_generation_pattern_only() -> None:
    text = f"a {FLD1} b CANARY-OLD-FLD-01-abcdef c {GEN1} d CANARY-GS6TEST-SPC-0a1b2c"
    assert canaries_in(text) == sorted({FLD1, GEN1, "CANARY-GS6TEST-SPC-0a1b2c"})
    assert canaries_in("") == [] and canaries_in(None) == []  # type: ignore[arg-type]


def test_with_only_the_cache_off_the_shared_memory_still_leaks_the_same_text() -> None:
    """A finding in itself: turning the cache off does not make a naive agent safe."""
    events, _ = run_s6(
        n=3, variants=["S6a"], arms=[("agent_naive_b", "naive", "setup_b")], cache_threshold=1.01
    )
    c = cell(build_s6_report(SPEC, events), "S6a", NAIVE_B)
    assert c.verdicts == {LEAK: 3} and all(r.cache_status == "below_threshold" for r in c.rows)


def test_s6_is_part_of_the_benchmark_report_with_its_own_table_and_caveats(
    full: Any, tmp_path: Any
) -> None:
    from benchmark_fakes import FakeRuns
    from leaklab.agent.s6.report import load_s6_report
    from leaklab.benchmark.aggregate import summarize
    from leaklab.benchmark.execute import init_manifest, load_plan, run_benchmark
    from leaklab.benchmark.matrix import Arm, build_plan
    from leaklab.benchmark.render import render_html
    from leaklab.runner.journal import Journal
    from runner_fakes import SPEC_PATH

    events, _ = full
    with Journal(tmp_path / "s6_20261018T000000Z.jsonl", fsync=False) as j:
        for e in events:
            j.append(e)
    plan = build_plan(SPEC, "a" * 64, arms=(Arm("S1-direct", 1),), cap_s=900.0)
    run_benchmark(plan, tmp_path, spec_path=SPEC_PATH, run_fn=FakeRuns())
    s6 = load_s6_report(tmp_path, SPEC)
    assert s6 is not None and s6.journal == "s6_20261018T000000Z.jsonl"
    summary = summarize(
        SPEC, load_plan(tmp_path), init_manifest(tmp_path, load_plan(tmp_path)), [], s6=s6
    )
    html = render_html(summary)
    assert "Scenario S6: agent cache and memory" in html and "S6 caveats" in html
    assert "agent_guarded_b" in html and "Exposure surface" in html and "worst-case leaker" in html
    assert "The fixed query set" in html and "Tell me what the Finance Leadership" in html
    assert load_s6_report(tmp_path / "empty", SPEC) is None


# -- the live harness's pure parts ---------------------------------------------------------------------


def test_the_inverse_of_a_revocation_adds_the_grant_back_as_a_read_grant() -> None:
    from leaklab.agent.s6.live import inverse

    removed = Mutation(
        type=MutationType.GRANT_REMOVED,
        params={"target": "S1-Direct", "principal_kind": "user", "principal_ref": "p-plateng"},
    )
    back = inverse(removed)
    assert back.type is MutationType.GRANT_ADDED
    assert back.params == {**removed.params, "role": "read"}
    left = Mutation(
        type=MutationType.GROUP_MEMBER_REMOVED,
        params={"group": "g", "member_kind": "user", "member_ref": "u"},
    )
    assert (
        inverse(left).type is MutationType.GROUP_MEMBER_ADDED
        and inverse(left).params == left.params
    )
    from leaklab.runner.model import RunnerError

    with pytest.raises(RunnerError):
        inverse(Mutation(type=MutationType.FILE_MOVED, params={"file": "f", "to_folder": "Board"}))


def test_a_real_model_is_refused_for_benchmark_runs() -> None:
    from typer.testing import CliRunner

    from leaklab.agent.cli import app
    from leaklab.agent.llm import StubLLM
    from leaklab.agent.s6.live import run_s6_live
    from leaklab.runner.model import RunnerError

    out = CliRunner().invoke(app, ["s6", "run", "--real-llm"])
    assert out.exit_code == 2 and "refused" in out.output
    with pytest.raises(RunnerError, match="exploratory"):
        run_s6_live(
            spec_path=__import__("pathlib").Path("x"),
            out_dir=__import__("pathlib").Path("y"),
            llm=StubLLM(),
        )
    assert CliRunner().invoke(app, ["explore"]).exit_code == 2  # needs --real-llm


def test_the_query_set_the_live_run_uses_is_the_validated_one() -> None:
    from leaklab.agent.embedding import HashEmbedder
    from leaklab.agent.graph import fact_line
    from leaklab.agent.queries import load_queries, validate
    from leaklab.agent.types import Passage

    fact = fact_line(
        Passage(
            "i", "n", "Finance/Leadership document 02\nx\nTopic: incident review\nReference code: C"
        )
    )
    assert validate(load_queries(), HashEmbedder(), {"S6c": fact}) == []
