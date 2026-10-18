"""The S6 report: a pure function of one S6 journal, with generated caveats. No numbers typed by hand.

Per variant x arm: trials, verdict counts (LEAK / OK / INVALID, SERVED / OVERBLOCK for the control),
the cache similarity of the critical question, guard denials, and the exposure surface (stored
canaries some persona may not read), which is reported next to the leaks but is not a leak count.
setup0 is listed as not applicable, with the reason from the journal.
"""

from collections import Counter
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from leaklab.agent.s6.scoring import (
    CACHE_KINDS,
    INVALID,
    LEAK,
    OK,
    OVERBLOCK,
    SERVED,
    TrialResult,
    score_trial,
    timeline_of,
)
from leaklab.runner import load_events
from leaklab.spec import TenantSpec
from leaklab.stats import Stat, stat

LABELS = {
    "agent_naive_a": "NAIVE agent, retrieval from Setup A (persona-trimmed, ACL-filtered index)",
    "agent_naive_b": "NAIVE agent, retrieval from Setup B (/query)",
    "agent_guarded_b": "GUARDED agent, retrieval from Setup B, access re-checked from Setup B's state",
}


class LeakRow(BaseModel):
    persona: str
    canary: str
    step: str


class TrialRow(BaseModel):
    trial: int
    verdict: str
    reason: str
    persona: str
    question: str
    similarity: float | None
    cache_status: str
    delivered: int  # canaries delivered at the critical step
    leaks: list[LeakRow]
    guard_denials: int


class S6Cell(BaseModel):
    variant: str
    kind: str
    arm: str
    trials: int
    verdicts: dict[str, int]
    leak_trials: int
    invalid_trials: int
    valid_trials: int
    similarity: Stat
    guard_denials: int
    exposure_stored: int
    exposure_unsafe: int
    exposure_pairs: int
    reasons: dict[str, int]
    rows: list[TrialRow]


class S6Report(BaseModel):
    journal: str
    generation: str | None
    query_version: int
    started_utc: str | None
    ended_utc: str | None
    llm: str
    embedder: str
    cache_threshold: float
    memory_threshold: float
    n: int
    settle_s: float
    arms: dict[str, str]
    not_applicable: dict[str, str]
    cells: list[S6Cell]
    questions: dict[str, dict[str, Any]]
    caveats: list[str]


def _utc(t: float | None) -> str | None:
    return None if t is None else datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%d %H:%M:%SZ")


def _cell(variant: str, kind: str, arm: str, results: Sequence[TrialResult]) -> S6Cell:
    return S6Cell(
        variant=variant,
        kind=kind,
        arm=arm,
        trials=len(results),
        verdicts=dict(Counter(r.verdict for r in results)),
        leak_trials=sum(1 for r in results if r.verdict == LEAK),
        invalid_trials=sum(1 for r in results if r.verdict == INVALID),
        valid_trials=sum(1 for r in results if r.verdict != INVALID),
        similarity=stat([r.similarity for r in results if r.similarity is not None]),
        guard_denials=sum(r.guard_denials for r in results),
        exposure_stored=sum(r.exposure_stored for r in results),
        exposure_unsafe=sum(r.exposure_unsafe for r in results),
        exposure_pairs=sum(r.exposure_pairs for r in results),
        reasons=dict(Counter(r.reason for r in results if r.verdict == INVALID)),
        rows=[
            TrialRow(
                trial=r.trial,
                verdict=r.verdict,
                reason=r.reason,
                persona=r.persona,
                question=r.question,
                similarity=r.similarity,
                cache_status=r.cache_status,
                delivered=len(r.delivered),
                leaks=[LeakRow(persona=x.persona, canary=x.canary, step=x.step) for x in r.leaks],
                guard_denials=r.guard_denials,
            )
            for r in sorted(results, key=lambda x: x.trial)
        ],
    )


def build_s6_report(
    spec: TenantSpec, events: Sequence[dict[str, Any]], journal: str = ""
) -> S6Report:
    started = next((e for e in events if e["type"] == "s6_started"), None)
    if started is None:
        raise ValueError("the journal has no s6_started event")
    generation = started.get("generation")
    if generation:
        spec = spec.with_generation(str(generation))
    queries = started["queries"]
    kinds = {name: v["kind"] for name, v in queries["variants"].items()}
    timeline = timeline_of(spec, events)
    grouped: dict[tuple[str, str], list[TrialResult]] = {}
    for e in events:
        if e["type"] != "s6_trial_started":
            continue
        variant, arm, trial = str(e["variant"]), str(e["arm"]), int(e["trial"])
        grouped.setdefault((variant, arm), []).append(
            score_trial(spec, events, variant, arm, trial, kinds[variant], timeline)
        )
    order = {v: i for i, v in enumerate(queries["variants"])}
    arm_order = {a["name"]: i for i, a in enumerate(started["arms"])}
    cells = [
        _cell(v, kinds[v], a, rs)
        for (v, a), rs in sorted(
            grouped.items(), key=lambda kv: (order[kv[0][0]], arm_order[kv[0][1]])
        )
    ]
    times = [float(e["t"]) for e in events if "t" in e and isinstance(e["t"], int | float)]
    report = S6Report(
        journal=journal,
        generation=str(generation) if generation else None,
        query_version=int(started["version"]),
        started_utc=_utc(float(started["t"])),
        ended_utc=_utc(max(times) if times else None),
        llm=str(started.get("llm", "stub")),
        embedder=str(started.get("embedder", "")),
        cache_threshold=float(queries["cache_threshold"]),
        memory_threshold=float(queries["memory_threshold"]),
        n=int(started["n"]),
        settle_s=float(started["settle_s"]),
        arms={a["name"]: LABELS.get(a["name"], a["name"]) for a in started["arms"]},
        not_applicable=dict(started.get("not_applicable", {})),
        cells=cells,
        questions={
            name: {"kind": v["kind"], "seed": v["seed"], "followups": v["followups"]}
            for name, v in queries["variants"].items()
        },
        caveats=[],
    )
    report.caveats = build_s6_caveats(report)
    return report


def build_s6_caveats(r: S6Report) -> list[str]:
    out = [
        f"LLM: {r.llm}. It answers by quoting its retrieved, cached and remembered text verbatim, "
        "so it is the worst-case leaker: every result measures the cache, the memory, the retrieval "
        "and the guard, not a language model. A real model is never used for these numbers.",
        f"Embedding: {r.embedder}, a deterministic local embedding (not a semantic model). Cache "
        f"threshold {r.cache_threshold:g}, memory threshold {r.memory_threshold:g}; the query set "
        f"(version {r.query_version}) was designed so that the paraphrases hit the cache and the "
        "shared-memory question hits only the memory. Other embeddings or thresholds would change how "
        "often a naive agent leaks.",
        f"Sample: {r.n} trials per variant per arm (follow-up i of a fixed query set), one tenant"
        + (f" (generation {r.generation})" if r.generation else "")
        + ". The trials are not independent draws: same documents, same users, a fixed question set.",
        "Arms: agent_naive_a retrieves from Setup A basic (ACL-filtered index: its stored ACL is frozen "
        "until the next indexer run, so after a revocation its retrieval may still return the "
        "document); agent_naive_b and agent_guarded_b retrieve through Setup B, which queries a "
        "separate, unfiltered index over the same SharePoint data source (Setup A uses the "
        "ACL-filtered index). The guarded arm decides from Setup B's state, which lags the tenant by "
        f"seconds: S6b waits {r.settle_s:g} s after the confirmed revocation before the stale-cache "
        "question.",
    ]
    for name, why in r.not_applicable.items():
        out.append(f"{name}: not applicable ({why}).")
    invalid = [c for c in r.cells if c.invalid_trials]
    if invalid:
        parts = "; ".join(
            f"{c.variant}/{c.arm}: {c.invalid_trials} ({', '.join(sorted(c.reasons))})"
            for c in invalid
        )
        out.append(
            "INVALID trials are trials in which the designed situation did not happen (no cache hit or "
            "memory hit for the critical question, a failed step, an unconfirmed revocation). They "
            "say nothing about the system and are never counted as OK; the rule applies to the guarded "
            f"arm as well, so that no-leak is only evidence when the guard faced a candidate. {parts}."
        )
    unsafe = sum(c.exposure_unsafe for c in r.cells)
    out.append(
        "Exposure surface (stored canaries that some persona may not read, in the cache or memory "
        f"at the end of a trial: {unsafe} over all cells) is what a leak could draw on. It is "
        "reported separately and is not a leak count: the guarded arm stores such answers too and "
        "never delivers them to a user who may not read their sources."
    )
    if r.started_utc and r.ended_utc:
        out.append(f"Dates: run between {r.started_utc} and {r.ended_utc} (UTC).")
    return out


def load_s6_report(folder: Path, spec: TenantSpec) -> S6Report | None:
    """The report of the newest s6_*.jsonl journal in the folder, or None."""
    journals = sorted(folder.glob("s6_*.jsonl"), key=lambda p: p.stat().st_mtime)
    if not journals:
        return None
    path = journals[-1]
    return build_s6_report(spec, load_events(path), path.name)


__all__ = [
    "S6Cell",
    "S6Report",
    "build_s6_report",
    "load_s6_report",
    "CACHE_KINDS",
    "OK",
    "SERVED",
    "OVERBLOCK",
]
