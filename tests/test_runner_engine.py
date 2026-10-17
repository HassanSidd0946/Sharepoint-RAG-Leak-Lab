"""The scenario engine end to end against the fake tenant and a fake system with injected faults.

Time is a FakeClock shared by the generator, the engine and the fake system, so a 15-minute baseline
or a 36-minute lag takes milliseconds. Numbers are asserted with the read interval as tolerance:
that is the resolution the real runner has too.
"""

from pathlib import Path
from typing import Any

import pytest

from generator_fakes import FakeClock, FakeTenant
from leaklab.generator import Env, GeneratorError, LogEntry
from leaklab.oracle import Mutation, MutationType
from leaklab.runner import (
    Engine,
    Journal,
    RunConfig,
    RunnerError,
    build_report,
    derive_plan,
    load_events,
    restore_env,
)
from leaklab.runner.metrics import RunReport, ScenarioResult
from leaklab.spec import TenantSpec, generate_canaries
from runner_fakes import SPEC, FakeSystem, make_env, make_world

CAN = generate_canaries(SPEC)
INTERVAL = 5.0
CONFIG = RunConfig(
    baseline_k=3,
    baseline_cap_s=300.0,
    n_consecutive=4,
    cap_s=900.0,
    interval_s=INTERVAL,
    steady_window_s=60.0,
)
World = tuple[FakeTenant, Env, FakeClock]
D1, D2 = CAN["S1-Direct"]
PLAT = CAN["Engineering/Platform-Secrets"][0]


def go(
    tmp_path: Path,
    scenarios: list[str],
    *,
    world: World | None = None,
    config: RunConfig = CONFIG,
    replay: list[dict[str, Any]] | None = None,
    path: Path | None = None,
    spec: TenantSpec = SPEC,
    **fake: Any,
) -> tuple[RunReport, FakeSystem, list[dict[str, Any]], World]:
    """Run the scenarios; with `replay`, continue that journal (as a new process would)."""
    tenant, env, clock = world or make_world()
    holder: dict[str, Engine] = {}
    system = FakeSystem(spec, clock, lambda: holder["engine"].applied, **fake)
    path = path or tmp_path / "run.jsonl"
    applied = restore_env(env, replay) if replay else []
    engine = holder["engine"] = Engine(
        env=env,
        spec=spec,
        systems=[system],
        config=config,
        journal=(journal := Journal(path, fsync=False)),
        applied=applied,
    )
    try:
        if not replay:
            journal.append(
                {
                    "type": "run_started",
                    "run_id": "t",
                    "scenarios": scenarios,
                    "systems": [system.name],
                    "config": config.as_dict(),
                }
            )
        engine.run(scenarios, replay=replay or [])
        journal.append({"type": "run_finished"})
    finally:
        journal.close()
    events = load_events(path)
    return build_report(spec, events), system, events, (tenant, env, clock)


def result(report: RunReport, scenario: str) -> ScenarioResult:
    return next(r for r in report.results if r.scenario == scenario)


def denied_after(value: float | None, lag: float) -> bool:
    """A system lagging `lag` seconds first refuses within one read interval after the lag."""
    return value is not None and lag - 0.01 <= value <= lag + INTERVAL + 0.1


def served_until(value: float | None, lag: float) -> bool:
    """...and its last served read is within one read interval before the lag ran out."""
    return value is not None and lag - INTERVAL - 0.1 <= value <= lag + 0.01


# -- the happy paths -----------------------------------------------------------------------------


def test_a_lagging_system_leaks_for_exactly_its_lag(tmp_path: Path) -> None:
    report, _, events, _ = go(tmp_path, ["S1-direct"], lag_s=25.0)
    r = result(report, "S1-direct")
    assert r.baseline_status == "BASELINE_OK" and r.mutation_status == "confirmed"
    assert r.end_reason == "converged" and r.convergence == "CONFORMED"
    assert r.verdict == "LEAK" and r.leak_reads >= 4
    assert denied_after(r.time_to_first_deny_s, 25.0)
    assert served_until(
        r.leak_exposure_s, 25.0
    )  # the last served read was one cycle before the denial
    assert denied_after(r.time_to_conformance_s, 25.0)
    assert r.errors == 0 and r.lockout_s is None and r.nothing_to_revoke == []
    assert [e["type"] for e in events].count("mutation") == 1
    assert report.finished and not report.notes


def test_a_perfect_system_has_no_exposure_and_still_needs_n_reads_after_the_change(
    tmp_path: Path,
) -> None:
    report, system, _, _ = go(tmp_path, ["S1-direct"], lag_s=0.0)
    r = result(report, "S1-direct")
    assert r.verdict == "OK" and r.leak_exposure_s == 0.0 and r.leak_reads == 0
    assert r.time_to_first_deny_s is not None and r.time_to_first_deny_s <= INTERVAL
    change = r.t_change
    assert change is not None
    reads_after = [t for t, pair in system.calls if pair == ("p-plateng", D1) and t >= change]
    # the streak from BEFORE the change must not count: n reads are needed after it
    assert len(reads_after) >= CONFIG.n_consecutive


def test_flapping_extends_the_exposure_and_the_tail_must_be_stable(tmp_path: Path) -> None:
    pair = ("p-plateng", D1)
    report, _, _, _ = go(tmp_path, ["S1-direct"], lag_s=10.0, flap={pair: 60.0})
    r = result(report, "S1-direct")
    p = next(x for x in r.pairs if (x.persona, x.canary) == pair)
    assert p.time_to_first_deny_s is not None and p.leak_exposure_s is not None
    assert p.leak_exposure_s > p.time_to_first_deny_s  # it was served again after a denial
    assert p.leak_exposure_s >= 60.0 + 10.0 - 2 * INTERVAL
    assert r.convergence == "CONFORMED"
    assert r.time_to_conformance_s is not None and r.time_to_conformance_s >= 60.0 + 10.0 - INTERVAL
    assert r.end_reason == "converged"


def test_the_three_s1_arms_share_one_polling_loop(tmp_path: Path) -> None:
    ids = ["S1-direct", "S1-spgroup", "S1-entra"]
    report, system, events, _ = go(
        tmp_path,
        ids,
        lag_by_pair={
            ("p-plateng", CAN["S1-Direct"][0]): 15.0,
            ("p-plateng", CAN["S1-Direct"][1]): 15.0,
            ("p-plateng", CAN["S1-SPGroup"][0]): 15.0,
            ("p-plateng", CAN["S1-SPGroup"][1]): 15.0,
            ("p-plateng", CAN["S1-Entra"][0]): 120.0,
            ("p-plateng", CAN["S1-Entra"][1]): 120.0,
        },
    )
    assert [e["type"] for e in events].count("group_started") == 1  # no interference: one group
    by = {r.scenario: r for r in report.results}
    assert denied_after(by["S1-direct"].time_to_first_deny_s, 15.0)
    assert denied_after(by["S1-spgroup"].time_to_first_deny_s, 15.0)
    assert denied_after(by["S1-entra"].time_to_first_deny_s, 120.0)
    assert all(
        r.end_reason == "converged" and r.baseline_status == "BASELINE_OK" for r in by.values()
    )
    # each unique pair is read once per cycle: the shared control is not read three times
    control = ("p-plateng", PLAT)
    cycles = {e["cycle"] for e in events if e["type"] == "obs"}
    per_cycle = [
        sum(
            1
            for e in events
            if e["type"] == "obs" and e["cycle"] == c and (e["persona"], e["canary"]) == control
        )
        for c in cycles
    ]
    assert set(per_cycle) == {1} and len(cycles) > 5
    del system
    # the mutations were applied one after the other, each with its own confirmed_at
    muts = [e for e in events if e["type"] == "mutation"]
    assert len(muts) == 3
    assert len({m["entry"]["confirmed_at"] for m in muts}) == 3
    # and a lag in one arm did not leak into the others' numbers
    assert by["S1-direct"].time_to_conformance_s is not None
    assert by["S1-direct"].time_to_conformance_s < by["S1-entra"].time_to_conformance_s  # type: ignore[operator]


def test_conflicting_scenarios_are_polled_in_separate_groups(tmp_path: Path) -> None:
    report, _, events, _ = go(
        tmp_path, ["S1-parent-revocation", "S2-nested-group-removal"], lag_s=0
    )
    assert [e["type"] for e in events].count("group_started") == 2
    s2 = result(report, "S2-nested-group-removal")
    revoked_canaries = {p.canary for p in s2.pairs if p.role == "revoking"}
    # S1 already removed the Engineering documents: S2 only has the General ones left to revoke
    assert revoked_canaries == set(CAN["General"])
    assert result(report, "S1-parent-revocation").end_reason == "converged"
    assert s2.end_reason == "converged"


def test_a_scenario_that_flips_nothing_is_watched_for_the_window_and_catches_a_leak(
    tmp_path: Path,
) -> None:
    special = CAN["special:f-direct-share"][0]
    report, system, _, _ = go(tmp_path, ["S4-move-into-restricted"], lag_s=0.0)
    r = result(report, "S4-move-into-restricted")
    assert r.end_reason == "steady_window_elapsed" and r.convergence == "NOT_APPLICABLE"
    assert r.verdict == "OK" and r.time_to_conformance_s is None
    assert r.t_change is not None
    assert max(t for t, _ in system.calls) - r.t_change >= CONFIG.steady_window_s
    # a system that serves the moved file to someone who must not read it, after the move
    leak_time = system.calls[0][0] + 30.0
    report2, _, _, _ = go(
        tmp_path / "b",
        ["S4-move-into-restricted"],
        leak_after={("p-intern", special): leak_time},
    )
    r2 = result(report2, "S4-move-into-restricted")
    assert r2.verdict == "LEAK" and r2.leak_reads > 0


# -- baseline ------------------------------------------------------------------------------------


def test_an_unstable_baseline_is_flagged_but_the_run_continues(tmp_path: Path) -> None:
    pair = ("p-plateng", D1)
    world = make_world()
    never_until = world[2].time() + 1000.0  # longer than the baseline cap
    report, _, events, _ = go(
        tmp_path,
        ["S1-direct"],
        world=world,
        deny_until={pair: never_until, ("p-plateng", D2): never_until},
    )
    r = result(report, "S1-direct")
    assert r.baseline_status == "BASELINE_UNSTABLE"
    assert r.mutation_status == "confirmed"  # it carried on
    assert r.nothing_to_revoke  # nothing was being served, so the revocation proves nothing
    assert r.lockout_s is not None and r.lockout_s >= CONFIG.baseline_cap_s - INTERVAL
    assert any("never became stable" in n for n in report.notes)
    assert any("nothing to revoke" in n for n in report.notes)
    done = next(e for e in events if e["type"] == "baseline_done")
    assert done["status"] == "BASELINE_UNSTABLE"


def test_errors_during_the_baseline_neither_pass_nor_fail_it(tmp_path: Path) -> None:
    report, _, _, _ = go(tmp_path, ["S1-direct"], error_every=7)
    r = result(report, "S1-direct")
    assert r.baseline_status == "BASELINE_OK"
    assert r.errors > 0 and r.verdict == "OK"  # errors are not denials: no lockout


# -- robustness ----------------------------------------------------------------------------------


def test_transport_errors_and_a_raising_adapter_never_stop_the_run(tmp_path: Path) -> None:
    report, _, events, _ = go(tmp_path, ["S1-direct"], lag_s=10.0, error_every=5, raise_every=11)
    r = result(report, "S1-direct")
    assert r.end_reason == "converged" and r.errors > 0
    assert r.verdict == "LEAK" and r.leak_reads > 0  # the lag is still measured
    errors = [e for e in events if e["type"] == "obs" and e["served"] is None]
    assert {e["error"] for e in errors} >= {"transport:ConnectError", "exception:RuntimeError"}
    assert all(e["served"] is not False for e in errors)  # never recorded as a denial


def test_a_system_that_never_revokes_ends_at_the_cap(tmp_path: Path) -> None:
    pair = ("p-plateng", D1)
    cfg = RunConfig(
        baseline_k=3, baseline_cap_s=300.0, n_consecutive=4, cap_s=120.0, interval_s=INTERVAL
    )
    report, _, _, _ = go(
        tmp_path, ["S1-direct"], config=cfg, never_revoke=[pair, ("p-plateng", D2)]
    )
    r = result(report, "S1-direct")
    assert r.end_reason == "cap" and r.convergence == "DID_NOT_CONVERGE"
    assert r.verdict == "LEAK" and r.time_to_first_deny_s is None
    assert len(r.pairs_never_denied) == 2
    assert r.leak_exposure_s is not None and r.leak_exposure_s >= cfg.cap_s - INTERVAL


def test_a_failed_mutation_ends_only_that_scenario(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import leaklab.runner.engine as engine_module

    real = engine_module.apply_mutation

    def flaky(env: Env, mutation: Mutation) -> LogEntry:
        if mutation.params.get("group") == "sp-s1-spgroup":
            raise GeneratorError("simulated failure")
        return real(env, mutation)

    monkeypatch.setattr(engine_module, "apply_mutation", flaky)
    report, _, events, _ = go(tmp_path, ["S1-direct", "S1-spgroup"], lag_s=0.0)
    broken, fine = result(report, "S1-spgroup"), result(report, "S1-direct")
    assert broken.mutation_status == "failed" and broken.end_reason == "mutation_failed"
    assert broken.convergence == "NOT_RUN"
    assert fine.end_reason == "converged"
    assert any(e["type"] == "mutation_failed" and "simulated" in e["error"] for e in events)


def test_an_unconfirmed_mutation_ends_the_scenario_without_scoring_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import leaklab.runner.engine as engine_module

    real = engine_module.apply_mutation

    def unconfirmed(env: Env, mutation: Mutation) -> LogEntry:
        entry = real(env, mutation)
        return entry.model_copy(update={"confirmed_at": None, "status": "unconfirmed"})

    monkeypatch.setattr(engine_module, "apply_mutation", unconfirmed)
    report, _, _, _ = go(tmp_path, ["S1-direct"])
    r = result(report, "S1-direct")
    assert r.end_reason == "mutation_unconfirmed" and r.mutation_status == "unconfirmed"
    assert r.convergence == "NOT_RUN" and r.t_change is None


def test_s6_is_refused_because_it_is_not_a_tenant_change(tmp_path: Path) -> None:
    with pytest.raises(RunnerError, match="no mutations"):
        go(tmp_path, ["S6-cached-answer"])


def test_parallel_reads_give_the_same_results(tmp_path: Path) -> None:
    cfg = RunConfig(
        baseline_k=3, baseline_cap_s=300.0, n_consecutive=4, cap_s=900.0, interval_s=INTERVAL,
        workers=4,
    )  # fmt: skip
    report, _, _, _ = go(tmp_path, ["S1-direct"], config=cfg, lag_s=25.0)
    r = result(report, "S1-direct")
    assert r.end_reason == "converged" and denied_after(r.time_to_first_deny_s, 25.0)


# -- crash and resume ------------------------------------------------------------------------------


def _reads_per_cycle(scenario: str) -> int:
    plan = derive_plan(SPEC, scenario)
    return len(plan.pairs)


@pytest.mark.parametrize("phase", ["baseline", "polling"])
def test_a_crashed_run_resumes_from_its_journal(tmp_path: Path, phase: str) -> None:
    per_cycle = _reads_per_cycle("S1-direct")
    crash_after = per_cycle * 2 + 3 if phase == "baseline" else per_cycle * 8 + 3
    world = make_world()
    path = tmp_path / "run.jsonl"
    with pytest.raises(KeyboardInterrupt):
        go(tmp_path, ["S1-direct"], world=world, path=path, lag_s=25.0, crash_after=crash_after)
    before = load_events(path)
    mutations_before = [e for e in before if e["type"] == "mutation"]
    assert bool(mutations_before) == (phase == "polling")
    last_cycle = max(e["cycle"] for e in before if e["type"] == "obs")

    # a new process: fresh Env (no caches) on the same tenant and clock
    tenant, _, clock = world
    env2 = make_env(tenant, clock)
    report, _, events, _ = go(
        tmp_path, ["S1-direct"], world=(tenant, env2, clock), path=path, replay=before, lag_s=25.0
    )
    r = result(report, "S1-direct")
    assert r.end_reason == "converged" and r.baseline_status == "BASELINE_OK"
    assert denied_after(r.time_to_first_deny_s, 25.0)
    assert [e["type"] for e in events].count("mutation") == 1  # applied once, never twice
    assert [e["type"] for e in events].count("group_resumed") == 1
    assert max(e["cycle"] for e in events if e["type"] == "obs") > last_cycle
    cycles = [e["cycle"] for e in events if e["type"] == "obs"]
    assert cycles == sorted(cycles)  # cycle numbers continue, they do not restart


def test_a_crash_inside_a_mutation_is_not_guessed_about(tmp_path: Path) -> None:
    path = tmp_path / "run.jsonl"
    go(tmp_path, ["S1-direct"], path=path)
    events = load_events(path)
    cut = next(i for i, e in enumerate(events) if e["type"] == "mutation")
    broken = [e for e in events[:cut] if e["type"] != "run_finished"]  # ends at mutation_started
    assert broken[-1]["type"] == "mutation_started"
    tenant, _, clock = make_world()
    with pytest.raises(RunnerError, match="may or may not have changed"):
        go(
            tmp_path / "again", ["S1-direct"],
            world=(tenant, make_env(tenant, clock), clock),
            replay=broken, path=tmp_path / "again.jsonl",
        )  # fmt: skip


def test_resume_restores_where_a_moved_file_now_is(tmp_path: Path) -> None:
    path = tmp_path / "run.jsonl"
    _, _, events, (_, env, _) = go(tmp_path, ["S4-move-into-restricted"])
    tenant, _, clock = make_world()
    env2 = make_env(tenant, clock)
    restore_env(env2, events)
    assert env2.special_folder["f-direct-share"] == "Board"
    assert env.special_folder["f-direct-share"] == "Board"
    assert len(env2.log.entries) == 1
    del path


def test_restore_env_skips_nothing_but_failed_entries() -> None:
    tenant, _, clock = make_world()
    env = make_env(tenant, clock)
    failed = LogEntry(
        seq=1,
        mutation=Mutation(
            type=MutationType.FILE_MOVED, params={"file": "f-direct-share", "to_folder": "Board"}
        ),
        requested_at=1.0,
        status="failed",
    )
    applied = restore_env(
        env, [{"type": "mutation", "scenario": "x", "entry": failed.model_dump(mode="json")}]
    )
    assert applied == [] and env.special_folder["f-direct-share"] == "General"


# -- errors must not look like success, and granting is measured too ------------------------------


def test_a_pair_that_only_ever_errors_after_the_change_never_converges(tmp_path: Path) -> None:
    """Errors are skipped, not counted as conforming: n error reads are not n conforming reads."""
    cfg = RunConfig(
        baseline_k=3, baseline_cap_s=300.0, n_consecutive=4, cap_s=120.0, interval_s=INTERVAL
    )
    world = make_world()
    start = world[2].time()
    # the revoked pairs start failing 12 s in: after the baseline (3 cycles) and before the change
    failing = {("p-plateng", D1): start + 12.0, ("p-plateng", D2): start + 12.0}
    report, _, _, _ = go(tmp_path, ["S1-direct"], world=world, config=cfg, error_pairs=failing)
    r = result(report, "S1-direct")
    assert r.end_reason == "cap" and r.convergence != "CONFORMED"
    assert r.verdict == "OK" and r.errors > 0  # never recorded as a denial or a leak
    assert r.time_to_first_deny_s is None and r.time_to_conformance_s is None


def spec_with_a_granting_scenario() -> TenantSpec:
    import copy

    import yaml

    from runner_fakes import SPEC_PATH

    with open(SPEC_PATH, encoding="utf-8") as fh:
        data: dict[str, Any] = copy.deepcopy(yaml.safe_load(fh))
    data["scenarios"].append(
        {
            "id": "X-grant",
            "description": "Give p-intern Read on Board (test only).",
            "mutations": [
                {
                    "type": "grant_added",
                    "params": {
                        "target": "Board",
                        "principal_kind": "user",
                        "principal_ref": "p-intern",
                        "role": "read",
                    },
                }
            ],
        }
    )
    return TenantSpec.model_validate(data)


def test_a_granting_scenario_measures_how_long_access_was_withheld(tmp_path: Path) -> None:
    spec = spec_with_a_granting_scenario()
    plan = derive_plan(spec, "X-grant")
    assert plan.granting == {("p-intern", c) for c in CAN["Board"]} and not plan.revoking
    report, _, _, _ = go(tmp_path, ["X-grant"], spec=spec, lag_s=30.0)
    r = result(report, "X-grant")
    assert r.end_reason == "converged" and r.convergence == "CONFORMED"
    # intent allowed p-intern from the change on, but the system only followed 30 s later
    assert r.verdict == "LOCKOUT" and r.lockout_reads > 0
    assert r.lockout_s is not None and 30.0 - INTERVAL <= r.lockout_s <= 30.0 + 0.01
    assert r.time_to_conformance_s is not None and denied_after(r.time_to_conformance_s, 30.0)
    assert r.time_to_first_deny_s is None and r.leak_exposure_s is None  # nothing was revoked
