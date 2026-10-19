"""The summary of a bundle: copied from the runner's report, recomputed from the bundle, asserted equal.

COPIED: for each system, the numbers of the scenario's ScenarioResult in the run's report file
(first denial, leak exposure, time to conformance, reads, errors, lockout reads).

RECOMPUTED: from nothing but the bundle's own `read` events, with the oracle's `classify()` over an
IntentTimeline built from the scenario's own mutation, then the same per-pair and per-scenario
assembly the report uses (runner.metrics). If the export lost, changed or invented a read, or if
another scenario's mutation touched this scenario's pairs, the two differ and the export FAILS
loudly (SummaryMismatch): the bundle must be able to reproduce the report it claims to come from.

STATUS: the report's convergence values map to the schema's status enum as CONFORMED -> CONVERGED;
DID_NOT_CONVERGE, INSUFFICIENT_DATA, NOT_APPLICABLE and (schema v1.1) NOTHING_TO_REVOKE (the revoked
document was not being served before the change) are kept as they are. The report's NO_DATA and NOT_RUN
have no place in the enum: they raise UnmappableStatus. A status is never renamed to one that says
something else (NOTHING_TO_REVOKE is not NOT_APPLICABLE).
"""

import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any

from leaklab.oracle import IntentTimeline
from leaklab.oracle.scoring import Observation as ScoredObservation
from leaklab.oracle.scoring import classify
from leaklab.runner.metrics import (
    NOT_RUN,
    NOTHING_TO_REVOKE,
    PairResult,
    _aggregate_convergence,
    _max,
    _pair_result,
)
from leaklab.runner.model import Observation

STATUS_MAP = {
    "CONFORMED": "CONVERGED",
    "DID_NOT_CONVERGE": "DID_NOT_CONVERGE",
    "INSUFFICIENT_DATA": "INSUFFICIENT_DATA",
    "NOT_APPLICABLE": "NOT_APPLICABLE",
    "NOTHING_TO_REVOKE": "NOTHING_TO_REVOKE",  # schema v1.1: never served, so nothing to revoke
}
FIELDS = (
    "first_deny_s",
    "leak_exposure_s",
    "time_to_conformance_s",
    "n_reads",
    "n_errors",
    "lockouts",
)


class UnmappableStatus(Exception):
    """The report holds a convergence value the schema's status enum has no place for."""

    def __init__(self, value: str, system: str) -> None:
        super().__init__(
            f"convergence {value!r} of {system} cannot be expressed in the schema's status enum "
            f"({sorted(set(STATUS_MAP.values()))}); it is not renamed to anything else"
        )
        self.value, self.system = value, system


class SummaryMismatch(Exception):
    """The summary copied from the report differs from the one recomputed from the bundle."""


def map_status(value: str, system: str) -> str:
    try:
        return STATUS_MAP[value]
    except KeyError:
        raise UnmappableStatus(value, system) from None


def from_report(
    results: Sequence[Mapping[str, Any]], scenario: str, systems: Sequence[str]
) -> list[dict[str, Any]]:
    """The scenario's per-system numbers as the report states them (raw convergence kept)."""
    out: list[dict[str, Any]] = []
    by_system = {r["system"]: r for r in results if r["scenario"] == scenario}
    for system in systems:
        r = by_system.get(system)
        if r is None:
            raise SummaryMismatch(f"the report has no result for {scenario}/{system}")
        out.append(
            {
                "system": system,
                "convergence": r["convergence"],
                "first_deny_s": r["time_to_first_deny_s"],
                "leak_exposure_s": r["leak_exposure_s"],
                "time_to_conformance_s": r["time_to_conformance_s"],
                "n_reads": r["observations"],
                "n_errors": r["errors"],
                "lockouts": r["lockout_reads"],
            }
        )
    return out


def recompute(
    reads: Sequence[Mapping[str, Any]],
    roles: Mapping[tuple[str, str], str],
    timeline: IntentTimeline,
    t_change: float,
    systems: Sequence[str],
    n_consecutive: int,
    cap_s: float,
) -> list[dict[str, Any]]:
    """The per-system summary from the bundle's `read` events alone (see the module docstring).

    `roles` maps (persona, canary) to revoking / granting / control / targeted.
    """
    out: list[dict[str, Any]] = []
    for system in systems:
        mine = [r for r in reads if r["system"] == system]
        scored = [
            ScoredObservation(
                system=system,
                persona=r["persona"],
                canary=r["canary"],
                t=r["t"],
                served=r["outcome"] == "served",
            )
            for r in mine
            if r["outcome"] in ("served", "denied")
        ]
        cases = {
            (c.persona, c.canary): c
            for c in classify(scored, timeline, n_consecutive=n_consecutive, cap_s=cap_s)
        }
        by_pair: dict[tuple[str, str], list[Observation]] = defaultdict(list)
        for r in mine:
            by_pair[(r["persona"], r["canary"])].append(
                Observation(
                    system=system,
                    persona=r["persona"],
                    canary=r["canary"],
                    ts=r["t"],
                    served={"served": True, "denied": False}.get(r["outcome"]),
                    error=None
                    if r["outcome"] in ("served", "denied")
                    else (r.get("error_kind") or r["outcome"]),
                )
            )
        times = [t_change]
        pairs: list[PairResult] = [
            _pair_result(pair, role, by_pair.get(pair, []), cases.get(pair), timeline, times)
            for pair, role in sorted(roles.items())
        ]
        flipped = [p for p in pairs if p.role in ("revoking", "granting")]
        convergence, ttc = _aggregate_convergence(flipped)
        all_revoking = [p for p in pairs if p.role == "revoking"]
        revoking = [p for p in all_revoking if p.convergence != NOTHING_TO_REVOKE]
        never = [p for p in revoking if p.time_to_first_deny_s is None]
        out.append(
            {
                "system": system,
                "convergence": convergence if times else NOT_RUN,
                "first_deny_s": None
                if never or not revoking
                else _max([p.time_to_first_deny_s for p in revoking]),
                "leak_exposure_s": _max([p.leak_exposure_s for p in revoking]),
                "time_to_conformance_s": ttc,
                "n_reads": sum(p.reads for p in pairs),
                "n_errors": sum(p.errors for p in pairs),
                "lockouts": sum(p.lockout_reads for p in pairs),
            }
        )
    return out


def _same(a: Any, b: Any) -> bool:
    if a is None or b is None:
        return a is b
    if isinstance(a, float) or isinstance(b, float):
        return math.isclose(float(a), float(b), rel_tol=0.0, abs_tol=1e-6)
    return bool(a == b)


def assert_equal(
    copied: Sequence[Mapping[str, Any]], recomputed: Sequence[Mapping[str, Any]], where: str
) -> None:
    """Fail loudly with every difference between the copied and the recomputed summary."""
    problems: list[str] = []
    mine = {r["system"]: r for r in recomputed}
    for c in copied:
        r = mine.get(c["system"])
        if r is None:
            problems.append(f"{c['system']}: not recomputed")
            continue
        for key in ("convergence", *FIELDS):
            if not _same(c[key], r[key]):
                problems.append(f"{c['system']}.{key}: report {c[key]!r} != recomputed {r[key]!r}")
    if problems:
        raise SummaryMismatch(
            f"{where}: the summary does not match the bundle's own reads:\n  "
            + "\n  ".join(problems)
        )


def to_schema(copied: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The summary in the schema's shape (status mapped; raises UnmappableStatus)."""
    return [
        {
            "system": c["system"],
            "status": map_status(str(c["convergence"]), str(c["system"])),
            **{k: c[k] for k in FIELDS},
        }
        for c in copied
    ]
