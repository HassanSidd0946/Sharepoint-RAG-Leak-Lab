"""The benchmark matrix and its plan: which scenarios run how often, packed into runs.

A RUN is one execution of the runner on a fresh tenant with all systems polled in the same loop.
Replicate number i of every scenario that still needs one goes into run i, so the 5 runs carry:
    S1-direct, S1-spgroup  runs 1-3        S1-entra, S2  runs 1-5        S4  runs 1-2
Scenarios of one run share a polling loop when their watched pairs are disjoint (runner.scenarios.
group_scenarios); the plan records that split and tells you when two scenarios have to wait for
each other. S2 has its own persona, groups and folder in the spec exactly so that it shares a loop
with S1-entra.

Baseline: S1-entra's baseline alternates between 1 and 10 minutes, run by run (odd runs 1, even
runs 10). The baseline is a property of the run (RunConfig.baseline_k conforming reads in a row,
expressed here in minutes), so every scenario in the run shares it; the report separates the
numbers by baseline length.

The plan is a pure function of the spec, the matrix and the poll settings: no network, no clock.
"""

import hashlib
import json
import math
from collections.abc import Sequence
from dataclasses import dataclass

from pydantic import BaseModel

from leaklab.runner import RunConfig, RunnerError, derive_plan, group_scenarios
from leaklab.spec import TenantSpec

BASELINE_SHORT_MIN = 1
BASELINE_LONG_MIN = 10
SYSTEMS = ("setup0", "setup_a_basic", "setup_b")


@dataclass(frozen=True)
class Arm:
    scenario: str
    replicates: int
    baseline_alternates: bool = False  # 1 min / 10 min, run by run


DEFAULT_ARMS = (
    Arm("S1-direct", 3),
    Arm("S1-spgroup", 3),
    Arm("S1-entra", 5, baseline_alternates=True),
    Arm("S2-nested-group-removal", 5),
    Arm("S4-move-into-restricted", 2),
)


def quick_arms(replicates: int = 1) -> tuple[Arm, ...]:
    """Every scenario of the default matrix, `replicates` times each (a verification set)."""
    return tuple(Arm(a.scenario, replicates, a.baseline_alternates) for a in DEFAULT_ARMS)


def parse_arms(specs: Sequence[str]) -> tuple[Arm, ...]:
    """ "S1-direct=3" -> Arm("S1-direct", 3). A scenario of the default matrix keeps its baseline
    alternation; any other scenario runs with the plain baseline."""
    alternates = {a.scenario: a.baseline_alternates for a in DEFAULT_ARMS}
    arms: list[Arm] = []
    for spec in specs:
        name, sep, count = spec.partition("=")
        if not sep or not count.strip().isdigit() or int(count) < 1:
            raise RunnerError(f"--arm needs SCENARIO=N with N >= 1, got {spec!r}")
        if any(a.scenario == name for a in arms):
            raise RunnerError(f"--arm {name!r} given twice")
        arms.append(Arm(name, int(count), alternates.get(name, False)))
    return tuple(arms)


class PlannedRun(BaseModel):
    index: int  # 1-based
    name: str  # "r1", "r2", ...
    scenarios: list[str]
    loops: list[list[str]]  # scenarios that share a polling loop; loops run one after the other
    baseline_minutes: int
    config: dict[str, float]  # RunConfig.as_dict()
    max_polling_s: float  # upper bound of the polling phases (not teardown / generate / settle)


class PlannedArm(BaseModel):
    scenario: str
    replicates: int
    baseline_alternates: bool


class BenchmarkPlan(BaseModel):
    version: int = 1
    spec_sha256: str
    systems: list[str]
    arms: list[PlannedArm]
    runs: list[PlannedRun]
    notes: list[str]

    def sha256(self) -> str:
        """A fingerprint of the plan: a manifest is only valid for the plan it was made for."""
        text = json.dumps(self.model_dump(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(text.encode()).hexdigest()

    @property
    def max_total_polling_s(self) -> float:
        return sum(r.max_polling_s for r in self.runs)


def baseline_minutes(arms: list[Arm], scenarios: list[str], run_index: int) -> int:
    """Alternating arms in the run decide (odd run: short, even run: long); else the short one."""
    alternating = any(a.baseline_alternates for a in arms if a.scenario in scenarios)
    if alternating and run_index % 2 == 0:
        return BASELINE_LONG_MIN
    return BASELINE_SHORT_MIN


def run_config(
    minutes: int,
    *,
    interval_s: float,
    n_consecutive: int,
    cap_s: float,
    steady_window_s: float,
    workers: int,
) -> RunConfig:
    """baseline_k = reads in a row that cover `minutes` at this poll interval; the cap gives the
    system 15 minutes beyond that to become stable before the baseline is flagged unstable."""
    return RunConfig(
        baseline_k=max(1, math.ceil(minutes * 60.0 / interval_s)),
        baseline_cap_s=minutes * 60.0 + 15 * 60.0,
        n_consecutive=n_consecutive,
        cap_s=cap_s,
        interval_s=interval_s,
        steady_window_s=steady_window_s,
        workers=workers,
    )


def build_plan(
    spec: TenantSpec,
    spec_sha256: str,
    *,
    arms: tuple[Arm, ...] = DEFAULT_ARMS,
    systems: tuple[str, ...] = SYSTEMS,
    interval_s: float = 5.0,
    n_consecutive: int = 10,
    cap_s: float = 3600.0,
    steady_window_s: float = 120.0,
    workers: int = 4,
) -> BenchmarkPlan:
    known = {s.id for s in spec.scenarios}
    for arm in arms:
        if arm.scenario not in known:
            raise RunnerError(
                f"the matrix names scenario {arm.scenario!r}; the spec has {sorted(known)}"
            )
        if arm.replicates < 1:
            raise RunnerError(f"{arm.scenario}: replicates must be at least 1")
    arm_list = list(arms)
    runs: list[PlannedRun] = []
    notes: list[str] = []
    for index in range(1, max(a.replicates for a in arms) + 1):
        scenarios = [a.scenario for a in arms if a.replicates >= index]
        plans = [derive_plan(spec, s) for s in scenarios]  # RunnerError if a scenario is unusable
        loops = [[p.id for p in group] for group in group_scenarios(plans)]
        minutes = baseline_minutes(arm_list, scenarios, index)
        config = run_config(
            minutes,
            interval_s=interval_s,
            n_consecutive=n_consecutive,
            cap_s=cap_s,
            steady_window_s=steady_window_s,
            workers=workers,
        )
        per_loop = [
            config.baseline_cap_s
            + (cap_s if any(derive_plan(spec, s).flipped for s in loop) else steady_window_s)
            for loop in loops
        ]
        runs.append(
            PlannedRun(
                index=index,
                name=f"r{index}",
                scenarios=scenarios,
                loops=loops,
                baseline_minutes=minutes,
                config=config.as_dict(),
                max_polling_s=sum(per_loop),
            )
        )
        if len(loops) > 1:
            notes.append(
                f"run {index}: scenarios cannot share one polling loop and run one after "
                f"the other: {loops}"
            )
    if not notes:
        notes.append(
            "every run polls all its scenarios in one loop (no scenario waits for another)"
        )
    return BenchmarkPlan(
        spec_sha256=spec_sha256,
        systems=list(systems),
        arms=[
            PlannedArm(
                scenario=a.scenario,
                replicates=a.replicates,
                baseline_alternates=a.baseline_alternates,
            )
            for a in arms
        ],
        runs=runs,
        notes=notes,
    )


def extend_plan(
    spec: TenantSpec, plan: BenchmarkPlan, scenarios: Sequence[str], count: int
) -> BenchmarkPlan:
    """The plan with `count` more runs of `scenarios` appended (the earlier runs are untouched).

    The new runs use the poll settings of the last planned run; the baseline keeps alternating by
    run number for scenarios that alternate (S1-entra), so run 6 is a 10-minute baseline, run 7 a
    1-minute one, and so on. Each scenario's replicate count in `arms` grows by `count`.
    """
    if count < 1 or not scenarios:
        raise RunnerError("extend needs at least one run and one scenario")
    known = {s.id for s in spec.scenarios}
    unknown = [s for s in scenarios if s not in known]
    if unknown:
        raise RunnerError(f"unknown scenario(s) {unknown}; the spec has {sorted(known)}")
    if not plan.runs:
        raise RunnerError("the plan has no runs to extend")
    cfg = plan.runs[-1].config
    arms = [Arm(a.scenario, a.replicates, a.baseline_alternates) for a in plan.arms]
    for scenario in scenarios:
        existing = next((i for i, a in enumerate(arms) if a.scenario == scenario), None)
        if existing is None:
            alternates = next(
                (a.baseline_alternates for a in DEFAULT_ARMS if a.scenario == scenario), False
            )
            arms.append(Arm(scenario, count, alternates))
        else:
            arms[existing] = Arm(
                scenario, arms[existing].replicates + count, arms[existing].baseline_alternates
            )
    plans = [derive_plan(spec, s) for s in scenarios]
    loops = [[p.id for p in group] for group in group_scenarios(plans)]
    runs = list(plan.runs)
    notes = list(plan.notes)
    for index in range(len(runs) + 1, len(runs) + count + 1):
        minutes = baseline_minutes(arms, list(scenarios), index)
        config = run_config(
            minutes,
            interval_s=cfg["interval_s"],
            n_consecutive=int(cfg["n_consecutive"]),
            cap_s=cfg["cap_s"],
            steady_window_s=cfg["steady_window_s"],
            workers=int(cfg["workers"]),
        )
        per_loop = [
            config.baseline_cap_s
            + (
                cfg["cap_s"]
                if any(derive_plan(spec, s).flipped for s in loop)
                else cfg["steady_window_s"]
            )
            for loop in loops
        ]
        runs.append(
            PlannedRun(
                index=index,
                name=f"r{index}",
                scenarios=list(scenarios),
                loops=loops,
                baseline_minutes=minutes,
                config=config.as_dict(),
                max_polling_s=sum(per_loop),
            )
        )
    if len(loops) > 1:
        notes.append(f"extension runs: scenarios cannot share one polling loop: {loops}")
    return BenchmarkPlan(
        spec_sha256=plan.spec_sha256,
        systems=list(plan.systems),
        arms=[
            PlannedArm(
                scenario=a.scenario,
                replicates=a.replicates,
                baseline_alternates=a.baseline_alternates,
            )
            for a in arms
        ],
        runs=runs,
        notes=notes,
    )
