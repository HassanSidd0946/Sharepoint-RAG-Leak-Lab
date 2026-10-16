"""Tests for oracle.scoring: LEAK / LOCKOUT / OK and time to conformance."""

from collections.abc import Collection, Mapping, Sequence

import pytest
from pydantic import ValidationError

from leaklab.oracle import (
    Convergence,
    Observation,
    StaticExpected,
    Verdict,
    classify,
    summarize,
)


class Revocation:
    """Intent: persona p may read canary c before `at`, and may not from `at` on."""

    def __init__(self, at: float = 10.0) -> None:
        self.at = at

    def expected_at(self, t: float) -> Mapping[str, Collection[str]]:
        return {"p": {"c"}} if t < self.at else {"p": set()}

    def change_times(self) -> Sequence[float]:
        return [self.at]


def obs(
    t: float, served: bool, system: str = "A", persona: str = "p", canary: str = "c"
) -> Observation:
    return Observation(system=system, persona=persona, canary=canary, t=t, served=served)


def reads(times_served: list[tuple[float, bool]]) -> list[Observation]:
    return [obs(t, served) for t, served in times_served]


# -- verdicts ------------------------------------------------------------------------------------


def test_ok_when_every_read_matches_intent() -> None:
    result = classify(
        reads([(1, True), (2, True), (11, False), (12, False)]), Revocation(), n_consecutive=2
    )
    [case] = result
    assert case.verdict is Verdict.OK
    assert case.leak_reads == case.lockout_reads == 0


def test_leak_when_served_after_intent_denies() -> None:
    [case] = classify(reads([(1, True), (11, True), (12, False)]), Revocation(), n_consecutive=1)
    assert case.verdict is Verdict.LEAK
    assert case.leak_reads == 1 and case.first_leak_t == 11


def test_a_read_exactly_at_the_change_time_is_judged_by_the_new_intent() -> None:
    [case] = classify(reads([(10, True)]), Revocation(10.0), n_consecutive=1)
    assert case.verdict is Verdict.LEAK


def test_lockout_when_withheld_while_intent_allows() -> None:
    [case] = classify(reads([(1, False), (2, True)]), StaticExpected({"p": {"c"}}), n_consecutive=1)
    assert case.verdict is Verdict.LOCKOUT
    assert case.lockout_reads == 1 and case.first_lockout_t == 1


def test_leak_wins_over_lockout() -> None:
    data = reads([(1, False), (11, True)])  # a lockout before, a leak after
    [case] = classify(data, Revocation(), n_consecutive=1)
    assert case.lockout_reads == 1 and case.leak_reads == 1
    assert case.verdict is Verdict.LEAK


def test_unknown_persona_or_canary_is_expected_to_be_denied() -> None:
    expected = StaticExpected({"p": {"c"}})
    cases = classify([obs(1, True, persona="stranger"), obs(1, True, canary="other")], expected)
    assert [c.verdict for c in cases] == [Verdict.LEAK, Verdict.LEAK]


def test_systems_personas_and_canaries_are_scored_separately() -> None:
    data = [obs(1, True, system="A"), obs(1, False, system="B"), obs(1, True, persona="q")]
    cases = classify(data, StaticExpected({"p": {"c"}}), n_consecutive=1)
    by = {(c.system, c.persona): c.verdict for c in cases}
    assert by == {("A", "p"): Verdict.OK, ("B", "p"): Verdict.LOCKOUT, ("A", "q"): Verdict.LEAK}


# -- time to conformance -----------------------------------------------------------------------


def revocation_reads() -> list[Observation]:
    """Allowed until 10, then served (leaking) at 11-13, then withheld from 14."""
    return reads(
        [(1, True), (2, True), (5, True), (11, True), (12, True), (13, True)]
        + [(14, False), (15, False), (16, False), (17, False)]
    )


def test_time_to_conformance_counts_from_the_change() -> None:
    [case] = classify(revocation_reads(), Revocation(10.0), n_consecutive=3)
    initial, after = case.segments
    assert initial.change_at is None and initial.status is Convergence.CONFORMED
    assert after.change_at == 10.0 and not after.expected_allowed
    assert after.status is Convergence.CONFORMED
    assert after.time_to_conformance_s == 4.0  # first conforming read of the final run is t=14
    assert case.final_time_to_conformance_s == 4.0
    assert case.verdict is Verdict.LEAK  # it leaked for a while


def test_cap_turns_a_late_convergence_into_did_not_converge() -> None:
    [late] = classify(revocation_reads(), Revocation(10.0), n_consecutive=3, cap_s=3.0)
    assert late.final_status is Convergence.DID_NOT_CONVERGE
    [ok] = classify(revocation_reads(), Revocation(10.0), n_consecutive=3, cap_s=4.0)
    assert ok.final_status is Convergence.CONFORMED


def test_too_few_conforming_reads_did_not_converge() -> None:
    [case] = classify(revocation_reads(), Revocation(10.0), n_consecutive=5)
    assert case.final_status is Convergence.DID_NOT_CONVERGE
    assert case.final_time_to_conformance_s is None


def test_a_relapse_after_a_conforming_run_does_not_count_by_default() -> None:
    data = reads([(5, True), (11, False), (12, False), (13, False), (14, True)])
    [strict] = classify(data, Revocation(10.0), n_consecutive=3)
    assert strict.final_status is Convergence.DID_NOT_CONVERGE
    [loose] = classify(data, Revocation(10.0), n_consecutive=3, require_stable_tail=False)
    assert loose.final_status is Convergence.CONFORMED
    assert loose.final_time_to_conformance_s == 1.0  # the run starts at t=11


def test_coin_flip_reads_do_not_converge() -> None:
    flips = [(11 + i, i % 2 == 0) for i in range(20)]
    [case] = classify(reads([(5, True)] + flips), Revocation(10.0), n_consecutive=5)
    assert case.final_status is Convergence.DID_NOT_CONVERGE
    assert case.verdict is Verdict.LEAK


def test_initial_segment_time_is_measured_from_the_first_read() -> None:
    data = reads([(100, False), (101, False), (102, True), (103, True), (104, True)])
    [case] = classify(data, StaticExpected({"p": {"c"}}), n_consecutive=3)
    [only] = case.segments
    assert only.change_at is None and only.time_to_conformance_s == 2.0
    assert case.verdict is Verdict.LOCKOUT


def test_a_change_that_does_not_affect_this_canary_does_not_split_the_segments() -> None:
    class OtherCanaryChanges:
        def expected_at(self, t: float) -> Mapping[str, Collection[str]]:
            return {"p": {"c", "d"} if t < 10 else {"c"}}

        def change_times(self) -> Sequence[float]:
            return [10.0]

    [case] = classify(reads([(5, True), (15, True)]), OtherCanaryChanges(), n_consecutive=1)
    assert len(case.segments) == 1


def test_changes_outside_the_observed_window_are_ignored() -> None:
    [case] = classify(reads([(1, True), (2, True)]), Revocation(50.0), n_consecutive=1)
    assert len(case.segments) == 1


# -- housekeeping -------------------------------------------------------------------------------


def test_n_consecutive_must_be_positive() -> None:
    with pytest.raises(ValueError):
        classify([obs(1, True)], StaticExpected({}), n_consecutive=0)


def test_observations_reject_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        Observation.model_validate(
            {"system": "A", "persona": "p", "canary": "c", "t": 1, "served": True, "extra": 1}
        )


def test_summarize_counts_per_system() -> None:
    data = [obs(1, True, system="A"), obs(1, False, system="B")]
    summary = summarize(classify(data, StaticExpected({"p": {"c"}}), n_consecutive=1))
    assert summary["A"]["OK"] == 1 and summary["B"]["LOCKOUT"] == 1
    assert summary["B"]["DID_NOT_CONVERGE"] == 1  # one lockout read, so no conforming run


def test_a_segment_shorter_than_n_is_insufficient_data_not_a_failure() -> None:
    [case] = classify(reads([(1, True), (2, True)]), StaticExpected({"p": {"c"}}), n_consecutive=3)
    assert case.final_status is Convergence.INSUFFICIENT_DATA
    assert case.final_time_to_conformance_s is None
    assert case.verdict is Verdict.OK
    assert summarize([case])["A"]["INSUFFICIENT_DATA"] == 1
