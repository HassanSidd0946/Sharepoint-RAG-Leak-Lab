"""Results computed from a hand-written journal, with numbers that can be checked by eye."""

from typing import Any

from leaklab.generator import LogEntry
from leaklab.oracle import Mutation, MutationType
from leaklab.runner import build_report, derive_plan
from leaklab.runner.scenarios import plan_to_dict
from leaklab.spec import generate_canaries
from runner_fakes import SPEC

CAN = generate_canaries(SPEC)
D1, D2 = CAN["S1-Direct"]
PLAT = CAN["Engineering/Platform-Secrets"][0]
CHANGE = 1000.0
CONFIG = {"n_consecutive": 3, "cap_s": 600.0}


def obs(
    persona: str, canary: str, ts: float, served: bool | None, cycle: int = 0
) -> dict[str, Any]:
    return {
        "type": "obs", "cycle": cycle, "system": "fake", "persona": persona, "canary": canary,
        "ts": ts, "served": served, "status": None, "final_status": None,
        "error": None if served is not None else "transport:ConnectError", "latency_ms": None,
    }  # fmt: skip


def header(scenarios: list[str] | None = None) -> list[dict[str, Any]]:
    ids = scenarios or ["S1-direct"]
    return [
        {
            "type": "run_started", "run_id": "r", "scenarios": ids, "systems": ["fake"],
            "config": CONFIG,
        },
        {
            "type": "group_started", "group": 0, "t": 890.0, "systems": ["fake"],
            "scenarios": [plan_to_dict(derive_plan(SPEC, s)) for s in ids],
        },
        {"type": "baseline_done", "scenario": ids[0], "status": "BASELINE_OK", "t": 995.0},
    ]  # fmt: skip


def mutation_event(confirmed: float | None = CHANGE) -> dict[str, Any]:
    entry = LogEntry(
        seq=1,
        mutation=Mutation(
            type=MutationType.GRANT_REMOVED,
            params={"target": "S1-Direct", "principal_kind": "user", "principal_ref": "p-plateng"},
        ),
        requested_at=confirmed or 0.0,
        executed_at=confirmed,
        confirmed_at=confirmed,
        status="confirmed" if confirmed is not None else "unconfirmed",
    )
    return {"type": "mutation", "scenario": "S1-direct", "entry": entry.model_dump(mode="json")}


def baseline_reads() -> list[dict[str, Any]]:
    """900..990 every 10 s: p-plateng is served both S1-Direct documents and the control."""
    rows = []
    for k, ts in enumerate(range(900, 1000, 10)):
        for canary in (D1, D2, PLAT):
            rows.append(obs("p-plateng", canary, float(ts), True, k))
        for persona in ("p-ceo", "p-finana", "p-finlead", "p-hr", "p-intern"):
            rows.append(obs(persona, D1, float(ts), False, k))
    return rows


def post_reads() -> list[dict[str, Any]]:
    rows = []
    # D1: served at 1010 and 1020, denied 1030, SERVED AGAIN 1040 (flap), denied from 1050
    d1 = {1010: True, 1020: True, 1030: False, 1040: True, 1050: False, 1060: False, 1070: False}
    # D2: served at 1010, 1020; denied from 1030 for good
    d2 = {1010: True, 1020: True, 1030: False, 1040: False, 1050: False, 1060: False, 1070: False}
    for k, ts in enumerate(range(1010, 1080, 10), start=10):
        rows.append(obs("p-plateng", D1, float(ts), d1[ts], k))
        rows.append(obs("p-plateng", D2, float(ts), d2[ts], k))
        rows.append(obs("p-plateng", PLAT, float(ts), ts != 1020, k))  # one lockout on the control
        for persona in ("p-ceo", "p-finana", "p-finlead", "p-hr", "p-intern"):
            rows.append(obs(persona, D1, float(ts), False, k))
    return rows


def full_journal() -> list[dict[str, Any]]:
    done = {"type": "scenario_done", "scenario": "S1-direct", "reason": "converged", "t": 1080.0}
    return [
        *header(), *baseline_reads(), {"type": "mutation_started", "scenario": "S1-direct"},
        mutation_event(), *post_reads(), done, {"type": "run_finished"},
    ]  # fmt: skip


def test_the_numbers_of_a_flapping_revocation_are_exactly_these() -> None:
    report = build_report(SPEC, full_journal())
    [r] = report.results
    assert r.scenario == "S1-direct" and r.system == "fake" and report.finished
    assert r.baseline_status == "BASELINE_OK" and r.mutation_status == "confirmed"
    assert r.end_reason == "converged" and r.t_change == CHANGE
    # D1 first refused at 1030 (30 s) but served again at 1040: exposure 40 s, stable only from 1050
    d1 = next(p for p in r.pairs if p.canary == D1 and p.persona == "p-plateng")
    assert (d1.time_to_first_deny_s, d1.leak_exposure_s, d1.time_to_conformance_s) == (
        30.0,
        40.0,
        50.0,
    )
    d2 = next(p for p in r.pairs if p.canary == D2)
    assert (d2.time_to_first_deny_s, d2.leak_exposure_s, d2.time_to_conformance_s) == (
        30.0,
        20.0,
        30.0,
    )
    # the scenario takes the worst pair for each number
    assert r.time_to_first_deny_s == 30.0
    assert r.leak_exposure_s == 40.0
    assert r.time_to_conformance_s == 50.0 and r.convergence == "CONFORMED"
    assert r.verdict == "LEAK" and r.leak_reads == 3 + 2  # D1: 1010, 1020, 1040; D2: 1010, 1020
    assert r.nothing_to_revoke == [] and r.pairs_never_denied == []
    assert r.read_interval_s == 10.0


def test_a_withheld_control_is_a_lockout_measured_from_the_start_of_the_allowed_stretch() -> None:
    [r] = build_report(SPEC, full_journal()).results
    control = next(p for p in r.pairs if p.role == "control")
    assert control.lockout_reads == 1 and control.verdict == "LOCKOUT"
    assert control.lockout_s == 120.0  # first read 900, the withheld read 1020
    assert r.lockout_s == 120.0


def test_errors_are_counted_and_are_in_none_of_the_numbers() -> None:
    events = full_journal()
    events.insert(-2, obs("p-plateng", D1, 1015.0, None, 10))
    events.insert(-2, obs("p-plateng", D1, 1016.0, None, 10))
    [r] = build_report(SPEC, events).results
    assert r.errors == 2
    d1 = next(p for p in r.pairs if p.canary == D1 and p.persona == "p-plateng")
    assert d1.time_to_first_deny_s == 30.0 and d1.leak_exposure_s == 40.0
    assert r.observations == len(
        [e for e in events if e["type"] == "obs" and (e["persona"], e["canary"]) in
         {(p.persona, p.canary) for p in r.pairs}]
    )  # fmt: skip


def test_a_pair_that_was_never_refused_has_no_first_deny_and_does_not_converge() -> None:
    events = [
        e
        for e in full_journal()
        if not (
            e["type"] == "obs"
            and e["ts"] >= 1030.0
            and e["canary"] == D2
            and e["persona"] == "p-plateng"
        )
    ]
    for ts in (1030.0, 1040.0, 1050.0, 1060.0, 1070.0):
        events.insert(-2, obs("p-plateng", D2, ts, True, 11))  # still served: never revoked
    [r] = build_report(SPEC, events).results
    assert r.time_to_first_deny_s is None and r.pairs_never_denied == [f"p-plateng:{D2}"]
    assert r.convergence == "DID_NOT_CONVERGE" and r.time_to_conformance_s is None
    assert r.leak_exposure_s == 70.0 and r.verdict == "LEAK"


def test_a_revoked_pair_that_was_not_served_before_the_change_has_nothing_to_revoke() -> None:
    events = header() + [
        obs("p-plateng", D1, float(ts), False, k) for k, ts in enumerate(range(900, 1000, 10))
    ]
    events += [mutation_event()]
    events += [obs("p-plateng", D1, float(ts), False, 20) for ts in range(1010, 1060, 10)]
    [r] = build_report(SPEC, events).results
    assert f"p-plateng:{D1}" in r.nothing_to_revoke
    # and its "revocation numbers" are not reported at all: nothing was served, nothing was revoked
    assert r.time_to_first_deny_s is None and r.leak_exposure_s is None
    assert r.convergence == "NOTHING_TO_REVOKE" and r.time_to_conformance_s is None
    assert r.pairs_never_denied == []
    assert {p.convergence for p in r.pairs if p.role == "revoking"} == {"NOTHING_TO_REVOKE"}
    for p in (p for p in r.pairs if p.role == "revoking"):  # also per pair, not only in the total
        assert p.time_to_first_deny_s is None and p.leak_exposure_s is None
    assert any("nothing to revoke" in n for n in build_report(SPEC, events).notes)
    assert r.lockout_s is not None  # it was denied while intent allowed it, before the change


def test_no_read_after_the_change_is_no_data_not_converged() -> None:
    events = [*header(), *baseline_reads(), mutation_event()]
    [r] = build_report(SPEC, events).results
    assert r.convergence == "NO_DATA" and r.time_to_conformance_s is None
    assert r.end_reason == "unfinished"
    report = build_report(SPEC, events)
    assert not report.finished
    assert any("interrupted" in n for n in report.notes)
    assert any("did not finish" in n for n in report.notes)


def test_an_unconfirmed_mutation_is_not_scored() -> None:
    events = [*header(), *baseline_reads(), mutation_event(None)]
    [r] = build_report(SPEC, events).results
    assert r.mutation_status == "unconfirmed" and r.convergence == "NOT_RUN" and r.t_change is None


def test_an_unstable_baseline_is_noted_and_judged_from_the_systems_own_reads() -> None:
    events = [
        e
        for e in full_journal()
        if not (e["type"] == "obs" and e["ts"] < 1000.0 and e["canary"] == PLAT)
    ]
    for k, ts in enumerate(range(900, 1000, 10)):
        events.insert(3, obs("p-plateng", PLAT, float(ts), False, k))  # denied while intent allowed
    events[2] = {"type": "baseline_done", "scenario": "S1-direct", "status": "BASELINE_UNSTABLE"}
    report = build_report(SPEC, events)
    assert report.results[0].baseline_status == "BASELINE_UNSTABLE"
    assert report.results[0].baseline_joint == "BASELINE_UNSTABLE"
    assert any("never became stable" in n for n in report.notes)


def test_the_baseline_is_judged_per_system_not_by_the_joint_decision() -> None:
    """Two systems share one baseline decision; a system that was fine must not inherit the other's
    failure (live case: setup_a_basic could not serve the SharePoint-group arm, setup0 could)."""
    events = full_journal()
    events[0] = {**events[0], "systems": ["good", "bad"]}
    events[2] = {"type": "baseline_done", "scenario": "S1-direct", "status": "BASELINE_UNSTABLE"}
    good = [{**e, "system": "good"} for e in events if e["type"] == "obs"]
    bad = [
        {**e, "system": "bad", "served": False if e["persona"] == "p-plateng" else e["served"]}
        for e in events
        if e["type"] == "obs"
    ]
    rest = [e for e in events if e["type"] != "obs"]
    report = build_report(SPEC, rest + good + bad)
    by = {r.system: r for r in report.results}
    assert by["good"].baseline_status == "BASELINE_OK"
    assert by["bad"].baseline_status == "BASELINE_UNSTABLE"
    assert by["good"].baseline_joint == by["bad"].baseline_joint == "BASELINE_UNSTABLE"
    notes = " ".join(report.notes)
    assert "S1-direct/bad" in notes and "S1-direct/good: the baseline" not in notes


def test_a_journal_without_a_header_is_refused() -> None:
    import pytest

    with pytest.raises(ValueError, match="run_started"):
        build_report(SPEC, [{"type": "obs"}])
