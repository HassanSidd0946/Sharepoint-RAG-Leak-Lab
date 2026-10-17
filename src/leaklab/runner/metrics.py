"""Turn a run journal into results. A pure function of the journal and the spec: no network.

Per (scenario, system) the report gives (all times in seconds, from the oracle's change time, i.e.
the `confirmed_at` of the scenario's mutation):
  time_to_first_deny_s    until the system FIRST refused a pair whose access was revoked; the
                          largest
                          over the revoked pairs (None if some revoked pair was never refused)
  leak_exposure_s         until the system LAST served a revoked pair after the change (flapping
                          counts: a pair served again after a denial extends the exposure); largest
                          over the revoked pairs. 0.0 means it never served one after the change
  time_to_conformance_s   until N conforming reads in a row that last to the end of the observation
                          (oracle.scoring.classify), largest over the pairs that flipped; or
                          DID_NOT_CONVERGE / INSUFFICIENT_DATA / NO_DATA
  lockout_s               the longest a system withheld a pair that intent allowed (measured from
                          the start of the allowed stretch to the last such read), over all pairs
  errors                  reads with served=None; they are in none of the numbers above
  baseline_status         BASELINE_OK / BASELINE_UNSTABLE / NOT_RUN
  observations            every read of the scenario's pairs, errors included
A revoked pair that was NOT being served just before the change has nothing to revoke: it is listed
in `nothing_to_revoke` and the revocation numbers of that scenario are not evidence of anything.
The resolution of every time is the read interval (see `read_interval_s`).
"""

from collections import defaultdict
from collections.abc import Sequence
from statistics import median
from typing import Any

from pydantic import BaseModel

from leaklab.generator import LogEntry
from leaklab.oracle import IntentTimeline, Mutation
from leaklab.oracle.scoring import CaseResult, Convergence, Verdict, classify
from leaklab.oracle.scoring import Observation as ScoredObservation
from leaklab.runner.journal import Event
from leaklab.runner.model import Observation, Pair
from leaklab.runner.scenarios import plan_from_dict
from leaklab.spec import TenantSpec

NOT_APPLICABLE = "NOT_APPLICABLE"  # nothing flipped (e.g. S4), so there is nothing to converge
NOT_RUN = "NOT_RUN"
NOTHING_TO_REVOKE = "NOTHING_TO_REVOKE"  # the pair was not being served before the change


class PairResult(BaseModel):
    persona: str
    canary: str
    role: str  # revoking | granting | targeted | control
    reads: int
    errors: int
    verdict: str
    leak_reads: int
    lockout_reads: int
    change_at: float | None
    served_before_change: bool | None
    time_to_first_deny_s: float | None
    leak_exposure_s: float | None
    lockout_s: float | None
    convergence: str
    time_to_conformance_s: float | None
    read_interval_s: float | None


class ScenarioResult(BaseModel):
    scenario: str
    system: str
    baseline_status: str  # judged for THIS system from its own reads (see _baseline_status)
    baseline_joint: str = NOT_RUN  # what the engine decided, across all systems, before mutating
    mutation_status: str  # confirmed | unconfirmed | failed | not_run
    end_reason: str  # converged | cap | steady_window_elapsed | mutation_failed | ... | unfinished
    t_change: float | None
    verdict: str
    convergence: str
    time_to_first_deny_s: float | None
    leak_exposure_s: float | None
    time_to_conformance_s: float | None
    lockout_s: float | None
    leak_reads: int
    lockout_reads: int
    errors: int
    observations: int
    read_interval_s: float | None
    nothing_to_revoke: list[str]
    pairs_never_denied: list[str]
    pairs: list[PairResult]
    # Only for systems that report indexer runs (setup_a_basic): how many runs ENDED after the
    # change and how long after it the first ended, to read next to time_to_first_deny_s.
    indexer_runs_after_change: int | None = None
    first_indexer_run_end_after_change_s: float | None = None


SETUP_B_LABEL = (
    "Setup B queries a separate, unfiltered index over the same SharePoint data source; "
    "Setup A uses the ACL-filtered index."
)
SYSTEM_LABELS: dict[str, str] = {"setup_b": SETUP_B_LABEL, "setup_a_basic": SETUP_B_LABEL}


class RunReport(BaseModel):
    run_id: str
    spec_sha256: str
    config: dict[str, Any]
    finished: bool
    results: list[ScenarioResult]
    notes: list[str]
    indexer_runs: dict[str, list[dict[str, Any]]] = {}
    # What each system is, in one sentence, for the systems of this run that need one.
    system_labels: dict[str, str] = {}
    # Setup B: results returned vs k for broad queries (the recall cost of post-filtering), and the
    # service's own health readings (ok/degraded, poller ages) as the run saw them.
    postfilter_recall: dict[str, list[dict[str, Any]]] = {}
    system_health: dict[str, list[dict[str, Any]]] = {}


def _flip_time(
    timeline: IntentTimeline, pair: Pair, times: Sequence[float], role: str
) -> float | None:
    """The first of `times` at which intent for `pair` flips the way `role` says."""
    persona, canary = pair
    want_allowed = role == "granting"
    for t in sorted(times):
        before = canary in timeline.expected_at(t - 1e-6).get(persona, ())
        after = canary in timeline.expected_at(t).get(persona, ())
        if before != after and after == want_allowed:
            return t
    return None


def _allowed_since(timeline: IntentTimeline, pair: Pair, ts: float, first_read: float) -> float:
    """Start of the allowed stretch containing `ts` (the pair's first read if it began allowed)."""
    persona, canary = pair
    start = first_read
    for t in timeline.change_times():
        if t > ts:
            break
        now = canary in timeline.expected_at(t).get(persona, ())
        was = canary in timeline.expected_at(t - 1e-6).get(persona, ())
        if now and not was:
            start = t
    return start


def _pair_result(
    pair: Pair,
    role: str,
    reads: list[Observation],
    case: CaseResult | None,
    timeline: IntentTimeline,
    times: Sequence[float],
) -> PairResult:
    scored = sorted((o for o in reads if o.served is not None), key=lambda o: o.ts)
    change = _flip_time(timeline, pair, times, role) if role in ("revoking", "granting") else None
    persona, canary = pair

    def allowed_at(t: float) -> bool:
        return canary in timeline.expected_at(t).get(persona, ())

    first_deny = leak_exposure = served_before = None
    if role == "revoking" and change is not None:
        after = [o for o in scored if o.ts >= change]
        denied = [o for o in after if o.served is False]
        first_deny = round(denied[0].ts - change, 3) if denied else None
        served_after = [o for o in after if o.served]
        if after:
            leak_exposure = round(served_after[-1].ts - change, 3) if served_after else 0.0
        before_reads = [o for o in scored if o.ts < change]
        served_before = bool(before_reads[-1].served) if before_reads else None

    nothing = role == "revoking" and change is not None and served_before is not True
    if nothing:  # nothing was served before the change: a "first denial" would be meaningless
        first_deny = leak_exposure = None
    lockout_s: float | None = None
    if scored:
        spans = [
            o.ts - _allowed_since(timeline, pair, o.ts, scored[0].ts)
            for o in scored
            if o.served is False and allowed_at(o.ts)
        ]
        lockout_s = round(max(spans), 3) if spans else None
    gaps = [b.ts - a.ts for a, b in zip(scored, scored[1:], strict=False)]
    # A flipped pair is judged on what happened AFTER the change; without a single scored read
    # after it, the old stretch must not be reported as "converged".
    judged = case is not None and (change is None or any(o.ts >= change for o in scored))
    return PairResult(
        persona=persona,
        canary=canary,
        role=role,
        reads=len(reads),
        errors=sum(1 for o in reads if o.served is None),
        verdict=case.verdict.value if case else Verdict.OK.value,
        leak_reads=case.leak_reads if case else 0,
        lockout_reads=case.lockout_reads if case else 0,
        change_at=change,
        served_before_change=served_before,
        time_to_first_deny_s=first_deny,
        leak_exposure_s=leak_exposure,
        lockout_s=lockout_s,
        convergence=(
            NOTHING_TO_REVOKE
            if nothing
            else case.final_status.value
            if case and judged
            else Convergence.NO_DATA.value
        ),
        time_to_conformance_s=(
            round(case.final_time_to_conformance_s, 3)
            if case and judged and not nothing and case.final_time_to_conformance_s is not None
            else None
        ),
        read_interval_s=round(median(gaps), 3) if gaps else None,
    )


_PRECEDENCE = [
    Convergence.DID_NOT_CONVERGE.value,
    Convergence.INSUFFICIENT_DATA.value,
    Convergence.NO_DATA.value,
    Convergence.CONFORMED.value,
]


def _aggregate_convergence(flipped: list[PairResult]) -> tuple[str, float | None]:
    if not flipped:
        return NOT_APPLICABLE, None
    judged = [p for p in flipped if p.convergence != NOTHING_TO_REVOKE]
    if not judged:
        return NOTHING_TO_REVOKE, None
    flipped = judged
    statuses = {p.convergence for p in flipped}
    for status in _PRECEDENCE:
        if status in statuses:
            if status != Convergence.CONFORMED.value:
                return status, None
            times = [
                p.time_to_conformance_s for p in flipped if p.time_to_conformance_s is not None
            ]
            return status, max(times) if times else None
    return Convergence.NO_DATA.value, None


def _baseline_status(
    pairs: Sequence[Pair],
    reads: dict[tuple[str, Pair], list[Observation]],
    system: str,
    timeline: IntentTimeline,
    until: float | None,
    k: int,
) -> str:
    """BASELINE_OK if, for EVERY pair, this system's last k scored reads before `until` all matched
    intent (errors skipped), else BASELINE_UNSTABLE. The engine's own baseline is joint across
    systems, so one system's trouble must not label another system's reads."""
    for persona, canary in pairs:
        scored = sorted(
            (
                o
                for o in reads.get((system, (persona, canary)), [])
                if o.served is not None and (until is None or o.ts < until)
            ),
            key=lambda o: o.ts,
        )
        recent = scored[-k:]
        if len(recent) < k:
            return "BASELINE_UNSTABLE"
        for o in recent:
            if o.served != (canary in timeline.expected_at(o.ts).get(persona, ())):
                return "BASELINE_UNSTABLE"
    return "BASELINE_OK"


def _max(values: Sequence[float | None]) -> float | None:
    real = [v for v in values if v is not None]
    return max(real) if real else None


def build_report(spec: TenantSpec, events: Sequence[Event]) -> RunReport:
    header = next((e for e in events if e["type"] == "run_started"), None)
    if header is None:
        raise ValueError("the journal has no run_started event")
    config = dict(header["config"])
    n = int(config["n_consecutive"])
    cap = float(config["cap_s"])

    applied: list[Mutation] = []
    entries: dict[str, list[LogEntry]] = defaultdict(list)
    plans: dict[str, Any] = {}
    baseline: dict[str, str] = {}
    done: dict[str, Event] = {}
    failed: set[str] = set()
    reads: dict[tuple[str, Pair], list[Observation]] = defaultdict(list)
    runs: dict[str, list[dict[str, Any]]] = defaultdict(list)
    recall: dict[str, list[dict[str, Any]]] = defaultdict(list)
    health: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in events:
        kind = e["type"]
        if kind == "system_event":
            payload = {k: v for k, v in e.items() if k != "type"}
            if e.get("kind") == "indexer_run":
                runs[str(e["system"])].append(payload)
            elif e.get("kind") == "postfilter_recall":
                recall[str(e["system"])].append(payload)
            elif e.get("kind") == "system_health":
                health[str(e["system"])].append(payload)
        elif kind == "group_started":
            for raw in e["scenarios"]:
                plans[str(raw["id"])] = plan_from_dict(spec, raw)
        elif kind == "mutation":
            entry = LogEntry.model_validate(e["entry"])
            entries[str(e["scenario"])].append(entry)
            if entry.status != "failed":
                applied.append(
                    entry.mutation.model_copy(update={"confirmed_at": entry.confirmed_at})
                )
        elif kind == "mutation_failed":
            failed.add(str(e["scenario"]))
        elif kind == "baseline_done":
            baseline[str(e["scenario"])] = str(e["status"])
        elif kind == "scenario_done":
            done[str(e["scenario"])] = e
        elif kind == "obs":
            obs = Observation.model_validate(
                {k: v for k, v in e.items() if k not in ("type", "cycle")}
            )
            reads[(obs.system, (obs.persona, obs.canary))].append(obs)

    timeline = IntentTimeline(spec, applied)
    notes: list[str] = []
    results: list[ScenarioResult] = []
    for system in [str(s) for s in header["systems"]]:
        scored = [
            ScoredObservation(system=system, persona=p, canary=c, t=o.ts, served=bool(o.served))
            for (sys_name, (p, c)), obs_list in reads.items()
            if sys_name == system
            for o in obs_list
            if o.served is not None
        ]
        cases = {
            (r.persona, r.canary): r for r in classify(scored, timeline, n_consecutive=n, cap_s=cap)
        }
        for sid, plan in plans.items():
            sc_entries = entries.get(sid, [])
            times = [e.confirmed_at for e in sc_entries if e.confirmed_at is not None]
            roles: dict[Pair, str] = {pair: "targeted" for pair in plan.targeted}
            roles.update({pair: "revoking" for pair in plan.revoking})
            roles.update({pair: "granting" for pair in plan.granting})
            if plan.control:
                roles.setdefault(plan.control, "control")
            pair_results = [
                _pair_result(
                    pair, role, reads.get((system, pair), []), cases.get(pair), timeline, times
                )
                for pair, role in sorted(roles.items())
            ]
            flipped = [p for p in pair_results if p.role in ("revoking", "granting")]
            convergence, ttc = _aggregate_convergence(flipped if times else [])
            all_revoking = [p for p in pair_results if p.role == "revoking"]
            revoking = [p for p in all_revoking if p.convergence != NOTHING_TO_REVOKE]
            never = [f"{p.persona}:{p.canary}" for p in revoking if p.time_to_first_deny_s is None]
            nothing = [
                f"{p.persona}:{p.canary}"
                for p in all_revoking
                if p.convergence == NOTHING_TO_REVOKE
            ]
            joint = baseline.get(sid, NOT_RUN)
            own_baseline = (
                NOT_RUN
                if joint == NOT_RUN
                else _baseline_status(
                    sorted(plan.pairs),
                    reads,
                    system,
                    timeline,
                    max(times) if times else None,
                    int(config.get("baseline_k", 5)),
                )
            )
            verdicts = {p.verdict for p in pair_results}
            verdict = (
                Verdict.LEAK.value
                if Verdict.LEAK.value in verdicts
                else Verdict.LOCKOUT.value
                if Verdict.LOCKOUT.value in verdicts
                else Verdict.OK.value
            )
            status = (
                "failed"
                if sid in failed
                else "not_run"
                if not sc_entries
                else sc_entries[-1].status
            )
            result = ScenarioResult(
                scenario=sid,
                system=system,
                baseline_status=own_baseline,
                baseline_joint=joint,
                mutation_status=status,
                end_reason=str(done[sid]["reason"]) if sid in done else "unfinished",
                t_change=max(times) if times else None,
                verdict=verdict,
                convergence=convergence if times else NOT_RUN,
                time_to_first_deny_s=(
                    None
                    if never or not revoking
                    else _max([p.time_to_first_deny_s for p in revoking])
                ),
                leak_exposure_s=_max([p.leak_exposure_s for p in revoking]),
                time_to_conformance_s=ttc,
                lockout_s=_max([p.lockout_s for p in pair_results]),
                leak_reads=sum(p.leak_reads for p in pair_results),
                lockout_reads=sum(p.lockout_reads for p in pair_results),
                errors=sum(p.errors for p in pair_results),
                observations=sum(p.reads for p in pair_results),
                read_interval_s=_max([p.read_interval_s for p in pair_results]),
                nothing_to_revoke=nothing,
                pairs_never_denied=never,
                pairs=pair_results,
            )
            if runs.get(system) and times:
                ended = sorted(
                    float(r["end"]) - max(times)
                    for r in runs[system]
                    if r.get("end") is not None and float(r["end"]) >= max(times)
                )
                result.indexer_runs_after_change = len(ended)
                result.first_indexer_run_end_after_change_s = round(ended[0], 3) if ended else None
            results.append(result)
            if result.baseline_status == "BASELINE_UNSTABLE":
                notes.append(
                    f"{sid}/{system}: the baseline never became stable; read the numbers with care"
                )
            if nothing and times:
                notes.append(
                    f"{sid}/{system}: {len(nothing)} revoked pair(s) were not being served just "
                    "before the change, so there was nothing to revoke"
                )
            if sid not in done:
                notes.append(
                    f"{sid}/{system}: the scenario did not finish (the run was interrupted)"
                )
    ran = sorted({str(s) for s in header.get("systems", [])})
    labels = {name: SYSTEM_LABELS[name] for name in ran if name in SYSTEM_LABELS}
    for name, samples in recall.items():
        short = [r for r in samples if int(r["shortfall"]) > 0]
        lost = sum(int(r["lost_vs_unlimited"]) for r in samples)
        notes.append(
            f"{name}: post-filter recall, {len(samples)} broad queries: {len(short)} returned "
            f"fewer than k, {lost} results lost against an unlimited window (see postfilter_recall)"
        )
    finished = any(e["type"] == "run_finished" for e in events)
    if not finished:
        notes.append(
            "the journal has no run_finished event: the run was interrupted or is still going"
        )
    return RunReport(
        run_id=str(header["run_id"]),
        spec_sha256=str(header.get("spec_sha256", "")),
        config=config,
        finished=finished,
        results=results,
        notes=notes,
        indexer_runs=dict(runs),
        system_labels=labels,
        postfilter_recall=dict(recall),
        system_health=dict(health),
    )
