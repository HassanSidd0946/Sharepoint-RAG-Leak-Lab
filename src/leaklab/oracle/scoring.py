"""Scoring: compare what a system served with what the oracle says it should have served.

Inputs: Observation records (system, persona, canary, time, served) and an expected-intent source
(IntentTimeline, or anything with expected_at(t) and change_times()). "served" means the canary
string appeared in what the system returned to that persona: exact string match, never an LLM
judgment.

Per (system, persona, canary) the result has:
  verdict  LEAK     the canary was served at least once while intent denied it,
           LOCKOUT  it was withheld at least once while intent allowed it (and never leaked),
           OK       every read matched intent.
           LEAK wins over LOCKOUT: a leak is the security failure, so it is never hidden.
           Reads right after an intent change count too: that is exactly the propagation window the
           lab measures. The segments below say how long it lasted.
  segments one per stretch of constant intent for that persona and canary (a new segment starts at a
           change time where the expected answer flips). For each: time_to_conformance_s, measured
           from the start of the segment to the first read that begins "N consecutive conforming
           reads", or DID_NOT_CONVERGE.

Convergence definition: with require_stable_tail=True (default) the conforming run must last to the
end of the segment, so a system that conforms for N reads and then flips back has NOT converged.
With False, any run of N conforming reads counts. cap_s (optional) turns a convergence later than
cap_s seconds after the segment start into DID_NOT_CONVERGE. A segment without reads is NO_DATA;
a segment with fewer than N reads is INSUFFICIENT_DATA (too short to judge, not a failure).

This module must not import engine code (see tests/test_oracle_isolation.py).
"""

from collections import defaultdict
from collections.abc import Collection, Iterable, Mapping, Sequence
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, ConfigDict


class Verdict(StrEnum):
    OK = "OK"
    LEAK = "LEAK"
    LOCKOUT = "LOCKOUT"


class Convergence(StrEnum):
    CONFORMED = "CONFORMED"
    DID_NOT_CONVERGE = "DID_NOT_CONVERGE"
    NO_DATA = "NO_DATA"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"  # fewer than n reads in the segment


class Observation(BaseModel):
    """One read: did `system` serve `canary` to `persona` at time `t`?"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    system: str
    persona: str
    canary: str
    t: float
    served: bool


class Segment(BaseModel):
    """A stretch of constant intent for one persona and canary."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    change_at: float | None  # None: the initial segment (starts at the first read)
    expected_allowed: bool
    reads: int
    conforming_reads: int
    status: Convergence
    time_to_conformance_s: float | None


class CaseResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    system: str
    persona: str
    canary: str
    verdict: Verdict
    reads: int
    leak_reads: int
    lockout_reads: int
    first_leak_t: float | None
    first_lockout_t: float | None
    segments: list[Segment]
    final_status: Convergence
    final_time_to_conformance_s: float | None


class ExpectedSource(Protocol):
    def expected_at(self, t: float) -> Mapping[str, Collection[str]]: ...

    def change_times(self) -> Sequence[float]: ...


class StaticExpected:
    """Intent that never changes (handy for tests and for runs without mutations)."""

    def __init__(self, expected: Mapping[str, Collection[str]]) -> None:
        self._expected = expected

    def expected_at(self, t: float) -> Mapping[str, Collection[str]]:
        return self._expected

    def change_times(self) -> Sequence[float]:
        return []


def _allowed(expected: ExpectedSource, persona: str, canary: str, t: float) -> bool:
    return canary in expected.expected_at(t).get(persona, ())  # unknown persona: deny


def _converge(
    conforming: list[bool],
    times: list[float],
    start: float,
    n: int,
    cap_s: float | None,
    require_stable_tail: bool,
) -> tuple[Convergence, float | None]:
    if not conforming:
        return Convergence.NO_DATA, None
    if len(conforming) < n:
        return Convergence.INSUFFICIENT_DATA, None
    first: int | None = None
    if require_stable_tail:
        j = len(conforming)
        while j > 0 and conforming[j - 1]:
            j -= 1
        if len(conforming) - j >= n:
            first = j
    else:
        run = 0
        for i, ok in enumerate(conforming):
            run = run + 1 if ok else 0
            if run >= n:
                first = i - n + 1
                break
    if first is None:
        return Convergence.DID_NOT_CONVERGE, None
    ttc = max(0.0, times[first] - start)
    if cap_s is not None and ttc > cap_s:
        return Convergence.DID_NOT_CONVERGE, None
    return Convergence.CONFORMED, ttc


def _classify_case(
    system: str,
    persona: str,
    canary: str,
    reads: list[Observation],
    expected: ExpectedSource,
    n: int,
    cap_s: float | None,
    require_stable_tail: bool,
) -> CaseResult:
    reads = sorted(reads, key=lambda o: o.t)
    allowed = [_allowed(expected, persona, canary, o.t) for o in reads]
    leaks = [o for o, a in zip(reads, allowed, strict=True) if o.served and not a]
    lockouts = [o for o, a in zip(reads, allowed, strict=True) if not o.served and a]

    # Segment boundaries: the first read, plus every change time (inside the observed window)
    # where the expected answer for this persona and canary flips.
    starts: list[tuple[float | None, float, bool]] = [(None, reads[0].t, allowed[0])]
    previous = allowed[0]
    for change in sorted(expected.change_times()):
        if reads[0].t < change <= reads[-1].t:
            now = _allowed(expected, persona, canary, change)
            if now != previous:
                starts.append((change, change, now))
                previous = now

    segments: list[Segment] = []
    for index, (change_at, start, expected_allowed) in enumerate(starts):
        end = starts[index + 1][1] if index + 1 < len(starts) else float("inf")
        members = [(o, a) for o, a in zip(reads, allowed, strict=True) if start <= o.t < end]
        conforming = [o.served == a for o, a in members]
        status, ttc = _converge(
            conforming, [o.t for o, _ in members], start, n, cap_s, require_stable_tail
        )
        segments.append(
            Segment(
                change_at=change_at,
                expected_allowed=expected_allowed,
                reads=len(members),
                conforming_reads=sum(conforming),
                status=status,
                time_to_conformance_s=ttc,
            )
        )

    verdict = Verdict.LEAK if leaks else Verdict.LOCKOUT if lockouts else Verdict.OK
    return CaseResult(
        system=system,
        persona=persona,
        canary=canary,
        verdict=verdict,
        reads=len(reads),
        leak_reads=len(leaks),
        lockout_reads=len(lockouts),
        first_leak_t=leaks[0].t if leaks else None,
        first_lockout_t=lockouts[0].t if lockouts else None,
        segments=segments,
        final_status=segments[-1].status,
        final_time_to_conformance_s=segments[-1].time_to_conformance_s,
    )


def classify(
    observations: Iterable[Observation],
    expected: ExpectedSource,
    *,
    n_consecutive: int = 10,
    cap_s: float | None = None,
    require_stable_tail: bool = True,
) -> list[CaseResult]:
    """Classify every (system, persona, canary) that has at least one observation."""
    if n_consecutive < 1:
        raise ValueError("n_consecutive must be at least 1")
    grouped: dict[tuple[str, str, str], list[Observation]] = defaultdict(list)
    for obs in observations:
        grouped[(obs.system, obs.persona, obs.canary)].append(obs)
    return [
        _classify_case(
            system, persona, canary, reads, expected, n_consecutive, cap_s, require_stable_tail
        )
        for (system, persona, canary), reads in sorted(grouped.items())
    ]


def summarize(results: Iterable[CaseResult]) -> dict[str, dict[str, int]]:
    """Counts per system: verdicts and how many cases did not converge."""
    out: dict[str, dict[str, int]] = defaultdict(
        lambda: {
            "OK": 0,
            "LEAK": 0,
            "LOCKOUT": 0,
            "DID_NOT_CONVERGE": 0,
            "INSUFFICIENT_DATA": 0,
            "cases": 0,
        }
    )
    for r in results:
        row = out[r.system]
        row["cases"] += 1
        row[r.verdict.value] += 1
        if r.final_status in (Convergence.DID_NOT_CONVERGE, Convergence.INSUFFICIENT_DATA):
            row[r.final_status.value] += 1
    return dict(out)
