"""S6 journal + benchmark report -> one `agent_trial` bundle per (variant, arm, trial), schema v1.1.

Inputs: the S6 journal named in report.json's `s6.journal` (runs/benchmark_v2/s6_*.jsonl), the
benchmark report.json (its `s6.cells[].rows[]` are the per-trial numbers the bundle's summary is
copied from) and tenant_spec.yaml.

WHERE EACH FIELD COMES FROM
  direct from the journal   the steps of a trial (who asked what, when), the cache / memory / guard
                            blocks of each step, the confirmed revocation of S6b, run settings
  derived                   path (cache_hit / memory_hit / retrieval), similarity, the canaries of the
                            delivered answer and their doc labels, expected per canary (ORACLE:
                            IntentTimeline over the journal's mutations, as the S6 scoring uses it),
                            turn verdict, guard decision and reason, validity
  copied from the report    summary (and recomputed with leaklab.agent.s6.scoring.score_trial; the two
                            must be equal or the export fails loudly)
  NOT exported              answer texts, drive item ids, user object ids (entry_owner) and the guard's
                            per-item results: they identify the tenant or add nothing the UI shows
"""

import json
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import jsonschema

from leaklab.agent.s6.scoring import TrialResult, canaries_in, score_trial, timeline_of
from leaklab.generator import LogEntry
from leaklab.replay.export import (
    EXPORTER_VERSION,
    SCHEMA_VERSION,
    TEST_TENANTS,
    Bundle,
    ExportError,
    ExportResult,
    Skipped,
    dur,
    git_commit,
    load_schema,
)
from leaklab.replay.labels import describe_mutation, doc_labels
from leaklab.replay.redact import assert_clean
from leaklab.replay.summary import SummaryMismatch
from leaklab.runner import load_events
from leaklab.spec import TenantSpec

ARM_LABELS = {
    "agent_naive_a": "Naive agent, retrieval A",
    "agent_naive_b": "Naive agent, retrieval B",
    "agent_guarded_b": "Guarded agent, retrieval B",
}
SCENARIO_OF = {"S6a": "S6a", "S6b": "S6b", "S6c": "S6c", "control": "positive_control"}


def _path(step: Mapping[str, Any]) -> str:
    if step.get("via") == "cache":
        return "cache_hit"
    if any(m.get("used") for m in step.get("memory") or []):
        return "memory_hit"
    return "retrieval"


def _similarity(step: Mapping[str, Any]) -> float | None:
    cache = step.get("cache") or {}
    if cache.get("served") or cache.get("candidate"):
        return float(cache["similarity"]) if cache.get("similarity") is not None else None
    sims = [
        float(m["similarity"]) for m in step.get("memory") or [] if m.get("similarity") is not None
    ]
    return max(sims) if sims else None


def _guard(step: Mapping[str, Any], mode: str) -> tuple[str, str | None]:
    entries = step.get("guard") or []
    if mode != "guarded" or not entries:
        return "n/a", None
    denied = [g for g in entries if not g.get("allowed", True)]
    if denied:
        return "denied", str(denied[0].get("reason"))
    return "allowed", str(entries[0].get("reason") or "allowed")


def _turn(
    step: Mapping[str, Any],
    t_first: float,
    mode: str,
    labels: Mapping[str, str],
    allowed: Mapping[str, set[str]],
) -> dict[str, Any]:
    answer = None if step.get("error") else (step.get("answer") or None)
    persona = str(step["persona"])
    delivered = []
    for canary in canaries_in(str(answer or "")):
        ok = canary in allowed.get(persona, set())
        delivered.append(
            {
                "canary": canary,
                "doc_label": labels.get(canary),
                "expected": "allow" if ok else "deny",
                "verdict": "ok" if ok else "leak",
            }
        )
    decision, reason = _guard(step, mode)
    verdict = (
        "unscored"
        if answer is None
        else ("leak" if any(d["verdict"] == "leak" for d in delivered) else "ok")
    )
    t1 = float(step["t1"])
    return {
        "t": t1,
        "t_rel_s": round(t1 - t_first, 3),
        "step": str(step["step"]),
        "persona": persona,
        "query": str(step["question"]),
        "path": _path(step),
        "similarity": _similarity(step),
        "source_doc_labels": sorted({d["doc_label"] for d in delivered if d["doc_label"]}),
        "delivered_canaries": delivered,
        "verdict": verdict,
        "guard_decision": decision,
        "guard_reason": reason,
    }


def _same_summary(row: Mapping[str, Any], result: TrialResult, where: str) -> None:
    """The report row and the score of the journal must say the same."""
    mine = {
        "verdict": result.verdict,
        "reason": result.reason,
        "similarity": result.similarity,
        "cache_status": result.cache_status,
        "delivered": len(result.delivered),
        "leaks": len(result.leaks),
        "guard_denials": result.guard_denials,
    }
    theirs = {
        "verdict": row["verdict"],
        "reason": row["reason"],
        "similarity": row["similarity"],
        "cache_status": row["cache_status"],
        "delivered": row["delivered"],
        "leaks": len(row["leaks"]),
        "guard_denials": row["guard_denials"],
    }
    diffs = [
        f"{k}: report {theirs[k]!r} != scored {mine[k]!r}" for k in mine if theirs[k] != mine[k]
    ]
    if diffs:
        raise SummaryMismatch(
            f"{where}: the S6 report row does not match the journal:\n  " + "\n  ".join(diffs)
        )


def export_agent(
    journal: Path,
    report_file: Path,
    spec: TenantSpec,
    *,
    spec_sha256: str,
    schema: dict[str, Any] | None = None,
    commit: str | None = None,
    exported_at: float | None = None,
) -> ExportResult:
    """All S6 trial bundles of one S6 journal. Raises SummaryMismatch / RedactionError / ValidationError."""
    schema = schema or load_schema()
    commit = commit if commit is not None else git_commit()
    exported_at = exported_at if exported_at is not None else time.time()
    report = json.loads(report_file.read_text(encoding="utf-8"))
    s6 = report.get("s6")
    if not s6 or s6.get("journal") != journal.name:
        raise ExportError(f"{report_file.name} has no s6 section for {journal.name}")
    events = load_events(journal)
    header = next((e for e in events if e["type"] == "s6_started"), None)
    if header is None:
        raise ExportError(f"{journal.name} has no s6_started event")
    if header.get("spec_sha256") != spec_sha256:
        raise ExportError(f"{journal.name} was recorded with another tenant_spec.yaml")
    generation = header.get("generation")
    if not generation:
        raise ExportError(f"{journal.name} names no tenant generation")
    spec_g = spec.with_generation(str(generation))
    labels = doc_labels(spec_g)
    timeline = timeline_of(spec_g, events)
    mode_of = {a["name"]: a["mode"] for a in header["arms"]}
    sources = {k: v.get("source") for k, v in header["queries"]["variants"].items()}
    personas = [{"id": u.id, "display": u.display} for u in spec.users if u.persona]
    run_id = journal.stem
    run_block = {
        "run_id": run_id,
        "generation": str(generation),
        "started_at": float(header["t"]),
        "llm": str(s6["llm"]),
        "embedder": str(s6["embedder"]),
        "cache_threshold": float(s6["cache_threshold"]),
        "memory_threshold": float(s6["memory_threshold"]),
        "personas": personas,
    }
    result = ExportResult()
    for cell in s6["cells"]:
        variant, arm, kind = str(cell["variant"]), str(cell["arm"]), str(cell["kind"])
        for row in cell["rows"]:
            trial = int(row["trial"])
            where = f"{run_id}/{variant}/{arm}/t{trial}"
            steps = sorted(
                (
                    e
                    for e in events
                    if e["type"] == "s6_step"
                    and (e["variant"], e["arm"], e["trial"]) == (variant, arm, trial)
                ),
                key=lambda e: float(e["t0"]),
            )
            if not steps:
                result.skipped.append(
                    Skipped(run_id, f"{variant}/{arm}/t{trial}", "no steps in the journal")
                )
                continue
            scored = score_trial(spec_g, events, variant, arm, trial, kind, timeline)
            _same_summary(row, scored, where)
            t_first = float(steps[0]["t0"])
            turns = [
                _turn(
                    s,
                    t_first,
                    mode_of[arm],
                    labels,
                    timeline.expected_at(float(s["t1"])),
                )
                for s in steps
            ]
            mutations = []
            for e in events:
                if e["type"] == "mutation" and (e.get("variant"), e.get("arm"), e.get("trial")) == (
                    variant,
                    arm,
                    trial,
                ):
                    entry = LogEntry.model_validate(e["entry"])
                    when = entry.confirmed_at or entry.executed_at or entry.requested_at
                    mutations.append(
                        {
                            "t": float(when),
                            "t_rel_s": round(float(when) - t_first, 3),
                            "summary": describe_mutation(spec_g, entry.mutation),
                        }
                    )
            scenario = SCENARIO_OF[variant]
            n = int(cell["trials"])
            data = {
                "schema_version": SCHEMA_VERSION,
                "kind": "agent_trial",
                "run": run_block,
                "trial": {
                    "scenario": scenario,
                    "kind": kind,
                    "arm": {"id": arm, "label": ARM_LABELS[arm], "mode": mode_of[arm]},
                    "n": n,
                    "trial": trial,
                    "question_source": str(sources.get(variant) or ""),
                },
                "turns": turns,
                "mutations": mutations,
                "validity": "INVALID" if scored.verdict == "INVALID" else "VALID",
                "summary": {
                    "verdict": row["verdict"],
                    "reason": row["reason"],
                    "similarity": row["similarity"],
                    "cache_status": row["cache_status"],
                    "delivered": row["delivered"],
                    "leaks": len(row["leaks"]),
                    "guard_denials": row["guard_denials"],
                    "exposure_stored": scored.exposure_stored,
                    "exposure_unsafe": scored.exposure_unsafe,
                    "exposure_pairs": scored.exposure_pairs,
                },
                "caveats": _caveats(run_block, s6, arm, scenario, n, len(labels)),
                "provenance": {
                    "journal_file": journal.name,
                    "report_file": report_file.name,
                    "exporter_version": EXPORTER_VERSION,
                    "git_commit": commit,
                    "exported_at": exported_at,
                },
            }
            jsonschema.Draft202012Validator(schema).validate(data)
            assert_clean(data, where)
            result.bundles.append(Bundle(f"{run_id}__{scenario}__{arm}__t{trial}", data))
    return result


def _caveats(
    run_block: Mapping[str, Any],
    s6: Mapping[str, Any],
    arm: str,
    scenario: str,
    n: int,
    n_docs: int,
) -> list[str]:
    tenants = "One test tenant" if TEST_TENANTS == 1 else f"{TEST_TENANTS} test tenants"
    return [
        f"{tenants}, {n_docs} synthetic documents (tenant generation {run_block['generation']}); "
        f"measured {s6['started_utc']} to {s6['ended_utc']} (UTC).",
        f"The language model is a stub that quotes its sources verbatim and the embedder is "
        f"{run_block['embedder']}; other models or embeddings change how often a naive agent leaks."
        if run_block["llm"] == "stub"
        else f"Language model: {run_block['llm']}; embedder: {run_block['embedder']}.",
        f"{s6['arms'][arm]}.",
        f"{n} trial{'s' if n != 1 else ''} per scenario and arm ({scenario}); descriptive only. A trial counts only if "
        "the cache or memory was actually hit with forbidden content; otherwise it is INVALID and never OK.",
        f"Cache similarity threshold {run_block['cache_threshold']:g}, memory threshold "
        f"{run_block['memory_threshold']:g}; the settle time between steps was {dur(float(s6['settle_s']))}.",
    ]


def export_agent_folder(
    runs: Path,
    spec: TenantSpec,
    spec_sha256: str,
    schema: dict[str, Any],
    commit: str,
    exported_at: float,
) -> ExportResult:
    """The S6 bundles of a benchmark folder: the journal its report.json names, if both exist."""
    report_file = runs / "report.json"
    if not report_file.exists():
        return ExportResult()
    report = json.loads(report_file.read_text(encoding="utf-8"))
    name = (report.get("s6") or {}).get("journal")
    if not name:
        return ExportResult()
    journal = runs / str(name)
    if not journal.exists():
        out = ExportResult()
        out.skipped.append(
            Skipped(str(name), "*", "the S6 journal named in report.json does not exist")
        )
        return out
    return export_agent(
        journal,
        report_file,
        spec,
        spec_sha256=spec_sha256,
        schema=schema,
        commit=commit,
        exported_at=exported_at,
    )


__all__ = ["ARM_LABELS", "export_agent", "export_agent_folder"]
