"""Journals -> a summary: per scenario x system statistics over the replicates, plus caveats.

Everything here is computed from the run journals (via runner.metrics.build_report) and the plan:
nothing is typed in by hand. A replicate is one scenario in one run. Rules, stated once:

  * A replicate is COMPLETE if its scenario ended by converging, by the poll cap, or by the end of
    the steady window. Unfinished or mutation-failed ones are listed but excluded from statistics.
  * first-deny and leak-exposure are statistics of the replicates that had something to revoke and
    gave a number. A replicate whose revoked pairs were never served just before the change
    (NOTHING_TO_REVOKE) is not evidence of anything and is counted separately. A replicate where a
    revoked pair was never refused has no first-deny time; it is counted in `never_denied` (the
    value is censored, it is NOT dropped silently).
  * min / median / max are descriptive. With 2-5 replicates they are not estimates of a
    distribution, and the caveats say so with the actual sample sizes.
  * DID_NOT_CONVERGE counts replicates that did not reach N conforming reads in a row within
    the cap.
"""

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from leaklab.agent.s6.report import S6Report
from leaklab.benchmark.execute import Manifest
from leaklab.benchmark.matrix import BenchmarkPlan
from leaklab.runner import build_report, load_events
from leaklab.runner.metrics import SETUP_B_LABEL, RunReport, ScenarioResult
from leaklab.setup_a.provision import MIN_SCHEDULE
from leaklab.spec import TenantSpec
from leaklab.stats import Stat, stat

COMPLETE_REASONS = frozenset({"converged", "cap", "steady_window_elapsed"})
NOT_EVIDENCE = "NOTHING_TO_REVOKE"
DID_NOT_CONVERGE = "DID_NOT_CONVERGE"
SMALL_N = 3  # a cell with fewer complete replicates than this is flagged in the caveats


class Replicate(BaseModel):
    run: int
    journal: str
    baseline_minutes: int
    baseline_status: str
    end_reason: str
    complete: bool
    verdict: str
    convergence: str
    first_deny_s: float | None
    leak_exposure_s: float | None
    conformance_s: float | None
    lockout_s: float | None
    errors: int
    observations: int
    nothing_to_revoke: bool
    never_denied: bool
    first_indexer_run_end_after_change_s: float | None


class BaselineSplit(BaseModel):
    baseline_minutes: int
    replicates: int
    first_deny: Stat
    leak_exposure: Stat


class Cell(BaseModel):
    scenario: str
    system: str
    planned: int  # replicates the plan asked for
    replicates: int  # complete replicates found
    incomplete: int  # in the journals but not complete
    first_deny: Stat
    leak_exposure: Stat
    conformance: Stat
    convergence: dict[str, int]
    did_not_converge: int
    verdicts: dict[str, int]
    lockout_replicates: int
    lockout_max_s: float | None
    errors: int
    observations: int
    baseline_status: dict[str, int]
    nothing_to_revoke: int
    never_denied: int
    first_indexer_run_end_after_change: Stat
    by_baseline: list[BaselineSplit]
    rows: list[Replicate]


class RunInfo(BaseModel):
    index: int
    name: str
    status: str
    journal: str | None
    baseline_minutes: int
    scenarios: list[str]
    started_utc: str | None
    ended_utc: str | None
    finished: bool
    error: str | None = None


class IndexerSummary(BaseModel):
    run: int
    system: str
    runs: int
    failed: int
    duration_s: Stat


class LagSummary(BaseModel):
    run: int
    system: str
    samples: int
    membership_age_s: Stat
    acl_age_s: Stat
    degraded_events: int


class StartupSummary(BaseModel):
    """Setup B's start-up in one run: when it was ready and whether its first sync was complete."""

    run: int
    ready: bool | None
    time_to_ready_s: float | None
    waited_in_prepare_s: float | None
    cap_s: float | None
    first_sync: dict[str, int]
    difference: dict[str, int]  # what the state had gained by readiness (empty: complete)
    complete_at_first_sync: bool | None
    found_by_verify: int | None


class CleanupIndex(BaseModel):
    index: str
    count_before: int | None
    count_after: int | None
    stale_found: int
    deleted: int
    failed: int


class CleanupSummary(BaseModel):
    """The index cleanup before one run (setup_a/cleanup.py), from the journal."""

    run: int
    generation: str | None
    error: str | None
    indexes: list[CleanupIndex]


class StaleSummary(BaseModel):
    run: int
    system: str
    stale_canaries: int  # canaries for which a stale copy (an earlier generation) was returned


class BenchmarkSummary(BaseModel):
    generated_utc: str
    spec_sha256: str
    plan_sha256: str
    systems: list[str]
    system_labels: dict[str, str]
    config: dict[str, float]
    cells: list[Cell]
    runs: list[RunInfo]
    indexer_runs: list[IndexerSummary]
    poller_lag: list[LagSummary]
    s6: S6Report | None = None  # Scenario S6 (agent cache and memory), if a run exists
    startup: list[StartupSummary] = []
    index_cleanups: list[CleanupSummary] = []
    stale_copies: list[StaleSummary] = []
    postfilter_recall: dict[str, Any]
    caveats: list[str]
    journals: list[str]


@dataclass
class LoadedRun:
    index: int
    name: str
    journal: str
    baseline_minutes: int
    events: list[dict[str, Any]]
    report: RunReport


def load_runs(
    folder: Path, plan: BenchmarkPlan, manifest: Manifest, spec: TenantSpec
) -> list[LoadedRun]:
    """Every run of the manifest that has a readable journal (finished or not)."""
    minutes = {r.index: r.baseline_minutes for r in plan.runs}
    loaded: list[LoadedRun] = []
    for entry in manifest.runs:
        if not entry.journal or not (folder / entry.journal).exists():
            continue
        events = [dict(e) for e in load_events(folder / entry.journal)]
        if not any(e["type"] == "run_started" for e in events):
            continue
        loaded.append(
            LoadedRun(
                index=entry.index,
                name=entry.name,
                journal=entry.journal,
                baseline_minutes=minutes[entry.index],
                events=events,
                report=build_report(spec, events),
            )
        )
    return loaded


def _replicate(run: LoadedRun, r: ScenarioResult) -> Replicate:
    complete = r.end_reason in COMPLETE_REASONS
    nothing = bool(r.nothing_to_revoke)
    return Replicate(
        run=run.index,
        journal=run.journal,
        baseline_minutes=run.baseline_minutes,
        baseline_status=r.baseline_status,
        end_reason=r.end_reason,
        complete=complete,
        verdict=r.verdict,
        convergence=r.convergence,
        first_deny_s=r.time_to_first_deny_s,
        leak_exposure_s=r.leak_exposure_s,
        conformance_s=r.time_to_conformance_s,
        lockout_s=r.lockout_s,
        errors=r.errors,
        observations=r.observations,
        nothing_to_revoke=nothing,
        never_denied=bool(r.pairs_never_denied) and not nothing,
        first_indexer_run_end_after_change_s=r.first_indexer_run_end_after_change_s,
    )


def _revocation_values(rows: Sequence[Replicate]) -> tuple[list[float], list[float]]:
    usable = [x for x in rows if x.complete and not x.nothing_to_revoke]
    return (
        [x.first_deny_s for x in usable if x.first_deny_s is not None],
        [x.leak_exposure_s for x in usable if x.leak_exposure_s is not None],
    )


def _cell(scenario: str, system: str, planned: int, rows: list[Replicate]) -> Cell:
    done = [x for x in rows if x.complete]
    deny, leak = _revocation_values(rows)
    convergence = Counter(x.convergence for x in done)
    splits = []
    for minutes in sorted({x.baseline_minutes for x in done}):
        part = [x for x in done if x.baseline_minutes == minutes]
        d, lk = _revocation_values(part)
        splits.append(
            BaselineSplit(
                baseline_minutes=minutes,
                replicates=len(part),
                first_deny=stat(d),
                leak_exposure=stat(lk),
            )
        )
    lockouts = [x.lockout_s for x in done if x.lockout_s is not None]
    return Cell(
        scenario=scenario,
        system=system,
        planned=planned,
        replicates=len(done),
        incomplete=len(rows) - len(done),
        first_deny=stat(deny),
        leak_exposure=stat(leak),
        conformance=stat([x.conformance_s for x in done if x.conformance_s is not None]),
        convergence=dict(convergence),
        did_not_converge=convergence.get(DID_NOT_CONVERGE, 0),
        verdicts=dict(Counter(x.verdict for x in done)),
        lockout_replicates=len(lockouts),
        lockout_max_s=max(lockouts) if lockouts else None,
        errors=sum(x.errors for x in done),
        observations=sum(x.observations for x in done),
        baseline_status=dict(Counter(x.baseline_status for x in done)),
        nothing_to_revoke=sum(1 for x in done if x.nothing_to_revoke),
        never_denied=sum(1 for x in done if x.never_denied),
        first_indexer_run_end_after_change=stat(
            [
                x.first_indexer_run_end_after_change_s
                for x in done
                if x.first_indexer_run_end_after_change_s is not None
            ]
        ),
        by_baseline=splits,
        rows=sorted(rows, key=lambda x: x.run),
    )


def _utc(t: float | None) -> str | None:
    return None if t is None else datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%d %H:%M:%SZ")


def _times(events: Sequence[dict[str, Any]]) -> tuple[float | None, float | None]:
    start = next((float(e["t"]) for e in events if e["type"] == "run_started" and "t" in e), None)
    ends = [float(e["t"]) for e in events if "t" in e and isinstance(e["t"], int | float)]
    ends += [float(e["ts"]) for e in events if e["type"] == "obs"]
    return start, (max(ends) if ends else None)


def _durations(runs: list[dict[str, Any]]) -> list[float]:
    return [float(r["end"]) - float(r["start"]) for r in runs if r.get("end") and r.get("start")]


def _indexer_and_lag(loaded: Sequence[LoadedRun]) -> tuple[list[IndexerSummary], list[LagSummary]]:
    indexer: list[IndexerSummary] = []
    lag: list[LagSummary] = []
    for run in loaded:
        for system, runs in sorted(run.report.indexer_runs.items()):
            failed = sum(1 for r in runs if r.get("status") != "success" or r.get("items_failed"))
            indexer.append(
                IndexerSummary(
                    run=run.index,
                    system=system,
                    runs=len(runs),
                    failed=failed,
                    duration_s=stat(_durations(runs)),
                )
            )
        samples = [
            e for e in run.events if e["type"] == "system_event" and e.get("kind") == "poller_lag"
        ]
        for system in sorted({str(e["system"]) for e in samples}):
            mine = [e for e in samples if e["system"] == system]
            degraded = sum(
                1
                for e in run.events
                if e["type"] == "system_event"
                and e.get("kind") == "system_health"
                and e.get("system") == system
                and e.get("status") != "ok"
            )
            lag.append(
                LagSummary(
                    run=run.index,
                    system=system,
                    samples=len(mine),
                    membership_age_s=stat(
                        [
                            float(e["membership_age_s"])
                            for e in mine
                            if e.get("membership_age_s") is not None
                        ]
                    ),
                    acl_age_s=stat(
                        [float(e["acl_age_s"]) for e in mine if e.get("acl_age_s") is not None]
                    ),
                    degraded_events=degraded,
                )
            )
    return indexer, lag


def _startup(loaded: Sequence[LoadedRun]) -> list[StartupSummary]:
    out: list[StartupSummary] = []
    for run in loaded:
        ready = [
            e
            for e in run.events
            if e["type"] == "system_event" and e.get("kind") == "setup_b_ready"
        ]
        start = [
            e
            for e in run.events
            if e["type"] == "system_event" and e.get("kind") == "setup_b_startup"
        ]
        if not ready:
            continue
        r = ready[0]
        s = start[0] if start else {}
        out.append(
            StartupSummary(
                run=run.index,
                ready=bool(r.get("ready")),
                time_to_ready_s=r.get("time_to_ready_s"),
                waited_in_prepare_s=r.get("waited_in_prepare_s"),
                cap_s=r.get("cap_s"),
                first_sync=dict(s.get("first_sync") or {}),
                difference=dict(s.get("difference") or {}),
                complete_at_first_sync=s.get("complete_at_first_sync"),
                found_by_verify=s.get("found_by_verify"),
            )
        )
    return out


def _cleanups(loaded: Sequence[LoadedRun]) -> list[CleanupSummary]:
    out: list[CleanupSummary] = []
    for run in loaded:
        for e in run.events:
            if e["type"] != "index_cleanup":
                continue
            error = e.get("error") or e.get("skipped")
            out.append(
                CleanupSummary(
                    run=run.index,
                    generation=e.get("generation"),
                    error=str(error) if error else None,
                    indexes=[_cleanup_index(i) for i in e.get("indexes", [])],
                )
            )
    return out


def _cleanup_index(i: dict[str, Any]) -> CleanupIndex:
    """An index that could be LISTED (the unfiltered one) is counted by search, which is exact;
    the ACL-filtered one shows nothing to the service token, so its documentCount statistic (which
    lags) is the only number there is."""
    if i.get("listed") is not None:
        before, after = i.get("count_search_before"), i.get("count_search_after")
    else:
        before, after = i.get("count_stats_before"), i.get("count_stats_after")
    return CleanupIndex(
        index=str(i["index"]),
        count_before=before,
        count_after=after,
        stale_found=int(i.get("stale_found") or 0),
        deleted=int(i.get("deleted") or 0),
        failed=int(i.get("failed") or 0),
    )


def _stale(loaded: Sequence[LoadedRun]) -> list[StaleSummary]:
    out: list[StaleSummary] = []
    for run in loaded:
        seen: dict[str, set[str]] = {}
        for e in run.events:
            if e["type"] == "system_event" and e.get("kind") == "stale_copy_seen":
                seen.setdefault(str(e["system"]), set()).add(str(e["canary"]))
        out += [
            StaleSummary(run=run.index, system=s, stale_canaries=len(c)) for s, c in seen.items()
        ]
    return out


def _recall(loaded: Sequence[LoadedRun]) -> dict[str, Any]:
    """Post-filter recall by k, over every sample of every run: returned vs the best possible."""
    samples = [
        e
        for run in loaded
        for e in run.events
        if e["type"] == "system_event" and e.get("kind") == "postfilter_recall"
    ]
    by_k: dict[int, dict[str, int]] = {}
    for e in samples:
        total = e.get("allowed_total")
        agg = by_k.setdefault(
            int(e["k"]),
            {
                "samples": 0,
                "returned": 0,
                "possible": 0,
                "short_of_k": 0,
                "lost": 0,
                "pages": 0,
                "max_pages": 0,
                "capped": 0,
            },
        )
        agg["samples"] += 1
        agg["returned"] += int(e["returned"])
        agg["possible"] += min(int(e["k"]), int(total)) if total is not None else int(e["returned"])
        agg["short_of_k"] += 1 if int(e["shortfall"]) > 0 else 0
        agg["pages"] += int(e.get("pages") or 1)
        agg["max_pages"] = max(agg["max_pages"], int(e.get("pages") or 1))
        agg["capped"] += 1 if e.get("capped") else 0
        agg["lost"] += int(e.get("lost_vs_unlimited") or 0)
    return {"samples": len(samples), "by_k": {str(k): v for k, v in sorted(by_k.items())}}


def summarize(
    spec: TenantSpec,
    plan: BenchmarkPlan,
    manifest: Manifest,
    loaded: Sequence[LoadedRun],
    *,
    now: datetime | None = None,
    s6: S6Report | None = None,
) -> BenchmarkSummary:
    grouped: dict[tuple[str, str], list[Replicate]] = {}
    for run in loaded:
        for result in run.report.results:
            grouped.setdefault((result.scenario, result.system), []).append(_replicate(run, result))
    cells = [
        _cell(arm.scenario, system, arm.replicates, grouped.get((arm.scenario, system), []))
        for arm in plan.arms
        for system in plan.systems
    ]
    by_index = {r.index: r for r in plan.runs}
    infos = []
    for entry in manifest.runs:
        found = next((x for x in loaded if x.index == entry.index), None)
        start, end = _times(found.events) if found else (None, None)
        infos.append(
            RunInfo(
                index=entry.index,
                name=entry.name,
                status=entry.status,
                journal=entry.journal,
                baseline_minutes=by_index[entry.index].baseline_minutes,
                scenarios=by_index[entry.index].scenarios,
                started_utc=_utc(start),
                ended_utc=_utc(end),
                finished=bool(found and found.report.finished),
                error=entry.error,
            )
        )
    indexer, lag = _indexer_and_lag(loaded)
    summary = BenchmarkSummary(
        generated_utc=(now or datetime.now(UTC)).strftime("%Y-%m-%d %H:%M:%SZ"),
        spec_sha256=plan.spec_sha256,
        plan_sha256=plan.sha256(),
        systems=plan.systems,
        system_labels={s: lab for r in loaded for s, lab in r.report.system_labels.items()},
        config=dict(plan.runs[0].config) if plan.runs else {},
        cells=cells,
        runs=infos,
        indexer_runs=indexer,
        poller_lag=lag,
        s6=s6,
        startup=_startup(loaded),
        index_cleanups=_cleanups(loaded),
        stale_copies=_stale(loaded),
        postfilter_recall=_recall(loaded),
        caveats=[],
        journals=sorted({r.journal for r in loaded}),
    )
    summary.caveats = build_caveats(spec, plan, summary, loaded)
    return summary


def build_caveats(
    spec: TenantSpec, plan: BenchmarkPlan, summary: BenchmarkSummary, loaded: Sequence[LoadedRun]
) -> list[str]:
    """The caveats of THIS data set, with its own numbers (no fixed claim about counts)."""
    out: list[str] = []
    cells = summary.cells
    ns = [c.replicates for c in cells]
    if ns:
        thin = [
            f"{c.scenario}/{c.system} (n={c.replicates})" for c in cells if c.replicates < SMALL_N
        ]
        asked = ", ".join(f"{a.scenario} {a.replicates}" for a in plan.arms)
        out.append(
            f"Sample sizes: each scenario x system cell has between {min(ns)} and {max(ns)} "
            f"complete replicates (planned: {asked}). Minimum, median and maximum are descriptive; "
            "with so few replicates they are not estimates of a distribution, and no confidence "
            "interval or significance test is given."
            + (f" Cells below {SMALL_N} replicates: {', '.join(thin)}." if thin else "")
        )
    out.append(
        "System labels: 'setup0' reads SharePoint itself (no search layer) and is the reference. "
        "'setup_a_basic' is Microsoft's SharePoint indexer with ACL ingestion in its plain "
        "configuration: users and Entra groups only, no SharePoint-group support, the smallest "
        f"indexer schedule the service accepts ({MIN_SCHEDULE}); it is not a tuned or best-case "
        f"Setup A. {SETUP_B_LABEL}"
    )
    personas = sorted(u.id for u in spec.users if u.persona)
    out.append(
        f"Scope: only the {len(personas)} persona accounts ({', '.join(personas)}) are measured. "
        "The tenant admin holds full control and appears in every ACL; access held by the admin "
        "is out of scope."
    )
    out.append(
        "Single tenant: every run used the same tenant, SharePoint site and search service. "
        "Results may not transfer to other tenants, sizes or configurations; the corpus is small "
        f"({len(_documents(spec))} documents)."
    )
    starts = [r.started_utc for r in summary.runs if r.started_utc]
    ends = [r.ended_utc for r in summary.runs if r.ended_utc]
    if starts and ends:
        out.append(
            f"Dates: runs between {min(starts)} and {max(ends)} (UTC); this report was generated "
            f"{summary.generated_utc}. Replicates ran one after another, not interleaved, so "
            "time-of-day and service-side drift are not controlled."
        )
    cfg = summary.config
    if cfg:
        out.append(
            f"Method: read interval {cfg.get('interval_s', 0.0):g} s (every time below has that "
            f"resolution); 'converged' means {int(cfg.get('n_consecutive', 0))} conforming "
            "reads in "
            f"a row; polling stopped {cfg.get('cap_s', 0.0) / 60:g} minutes after the change, so a "
            "DID_NOT_CONVERGE replicate is censored at the cap and its numbers are lower bounds. "
            "Every replicate started from a freshly generated tenant."
        )
    alternating = [a.scenario for a in plan.arms if a.baseline_alternates]
    if alternating:
        by_minutes: dict[int, list[int]] = {}
        for r in plan.runs:
            by_minutes.setdefault(r.baseline_minutes, []).append(r.index)
        parts = "; ".join(f"{m} min in runs {idx}" for m, idx in sorted(by_minutes.items()))
        out.append(
            f"Baseline: the baseline length alternates by run ({parts}) for "
            f"{', '.join(alternating)}. The other scenarios in a run share its baseline. "
            "The 'by baseline' rows separate the numbers."
        )
    out.extend(_data_quality(cells))
    out.extend(_startup_caveats(summary))
    errors = sum(c.errors for c in cells)
    if errors:
        out.append(
            f"Read errors: {errors} reads returned no answer (network, token, throttling); "
            "they are excluded from every number."
        )
    if any(c.system == "setup_b" for c in cells):
        out.append(
            "Setup B is measured through its HTTP API with the persona's own token. Its index is "
            "refreshed by its own indexer on its own 5-minute cycle, so content freshness is "
            "comparable to Setup A's but not simultaneous. Post-filtering over a k*5 window loses "
            f"results ({summary.postfilter_recall['samples']} recall samples, see the table)."
        )
    failed = [r for r in summary.runs if r.status == "failed"]
    if failed:
        out.append("Failed runs: " + "; ".join(f"run {r.index}: {r.error}" for r in failed) + ".")
    return out


def _startup_caveats(summary: BenchmarkSummary) -> list[str]:
    """What the journals say about Setup B's start-up, the index cleanup and stale copies."""
    out: list[str] = []
    if summary.startup:
        times = [s.time_to_ready_s for s in summary.startup if s.time_to_ready_s is not None]
        late = [s.run for s in summary.startup if not s.ready]
        incomplete = [s for s in summary.startup if s.complete_at_first_sync is False]
        text = (
            f"Setup B start-up: the runner waited for /ready before the baseline in "
            f"{len(summary.startup)} run(s); time to ready "
            + (
                f"{min(times):g}-{max(times):g} s (median {sorted(times)[len(times) // 2]:g} s)"
                if times
                else "unknown"
            )
            + ". Lockouts are counted only from reads after that point"
        )
        if late:
            text += f"; run(s) {late} were not ready within the cap and are counted anyway"
        text += f". The first sync was already complete in {len(summary.startup) - len(incomplete)}"
        text += f" of {len(summary.startup)} run(s)"
        if incomplete:
            gained = sorted({k for s in incomplete for k in s.difference})
            text += (
                f"; in {len(incomplete)} the state gained {', '.join(gained)} between the first "
                "sync and readiness (the first sync was incomplete)"
            )
        out.append(text + ".")
    if summary.index_cleanups:
        found = sum(i.stale_found for c in summary.index_cleanups for i in c.indexes)
        deleted = sum(i.deleted for c in summary.index_cleanups for i in c.indexes)
        failed = [c.run for c in summary.index_cleanups if c.error]
        text = (
            f"Index cleanup: before {len(summary.index_cleanups)} run(s) the documents of earlier "
            f"tenant generations were deleted from both indexes ({found} stale found, {deleted} "
            "deleted in total)"
        )
        if failed:
            text += f"; the cleanup did NOT complete in run(s) {failed}"
        out.append(text + ".")
    stale = sum(s.stale_canaries for s in summary.stale_copies)
    if stale:
        out.append(
            f"Stale copies: Setup A's index still returned a copy of {stale} canary text(s) from "
            "an "
            "earlier tenant generation; they were not counted as served or as evidence of "
            "indexing (matched on the current item id)."
        )
    return out


def _data_quality(cells: Sequence[Cell]) -> list[str]:
    counts = [
        (sum(c.did_not_converge for c in cells), "replicate(s) did not converge within the cap"),
        (
            sum(c.baseline_status.get("BASELINE_UNSTABLE", 0) for c in cells),
            "replicate(s) started from an unstable baseline for that system",
        ),
        (
            sum(c.nothing_to_revoke for c in cells),
            "replicate(s) had nothing to revoke (the pair was not served before the change) "
            "and are excluded from first-deny and leak statistics",
        ),
        (
            sum(c.never_denied for c in cells),
            "replicate(s) never refused a revoked pair (no first-deny time; censored)",
        ),
        (sum(c.incomplete for c in cells), "replicate(s) are incomplete and excluded"),
        (
            sum(max(0, c.planned - c.replicates - c.incomplete) for c in cells),
            "planned replicate(s) have no result yet (not run, or the run stopped early)",
        ),
    ]
    found = [f"{n} {text}" for n, text in counts if n]
    return ["Data quality: " + "; ".join(found) + "."] if found else []


def _documents(spec: TenantSpec) -> list[str]:
    from leaklab.spec import generate_canaries

    return [c for cs in generate_canaries(spec).values() for c in cs]
