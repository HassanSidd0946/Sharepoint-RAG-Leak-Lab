"""Journal + report -> replay bundles: one per (run, scenario, replicate), schema v1.0.

Inputs per run: the runner's journal (run_*.jsonl), the runner's per-run report next to it
(run_*.report.json, the only file with the per-replicate numbers the schema wants: lockouts and read
counts) and tenant_spec.yaml (checked against the sha256 the journal recorded). Output: dicts that
validate against docs/schemas/replay_bundle.schema.json and contain nothing that identifies the tenant.

WHERE EACH FIELD COMES FROM
  direct from the journal   run.generation, started_at, read_interval_s, poll_cap_s, n_consecutive,
                            systems[].id, every read's time / system / persona / canary / error_kind,
                            mutation times and type, indexer_run times / status / items, poller lag
                            ages (membership and acl, split into two events), the ready phase
  derived                   expected and verdict (ORACLE: IntentTimeline over the run's mutation log, as
                            the scoring uses it), t_rel_s, the read phase (baseline before the
                            confirmed change, poll after), outcome from served / error, status_code
                            (the status of the FINAL answer when a redirect happened), tracked_pairs
                            (the scenario's plan; roles flipped / control / other), doc_label, the
                            replicate number, baseline_target_s (baseline_k x read interval), the
                            phases baseline_start / baseline_stable / poll_start / first_deny /
                            converged / cap_reached, the mutation's text, personas' display names
  fixed by the exporter     system label and config_label (the journals do not hold them), the index
                            name of an indexer run (from the system id)
  copied from the report    summary (and recomputed from the bundle; they must be equal)

WHAT A JOURNAL CANNOT GIVE (left out, never made up)
  * indexer runs and poller lag exist only for the systems that report them (Azure AI Search / Setup B
    indexers, Setup B's pollers); a run without them has no such events;
  * an indexer run that was started but whose finish was never journalled keeps t_end null and the
    status "unfinished" (the journal does not say how it ended);
  * the `ready` phase exists only for Setup B (the only system with a readiness gate);
  * the schema's phase list has no value for the end of S4's steady window, so none is written.
"""

import json
import re
import subprocess
import time
from bisect import bisect_right
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import jsonschema

from leaklab.generator import LogEntry
from leaklab.oracle import IntentTimeline, Mutation
from leaklab.replay.labels import INDEX_OF, SYSTEMS, describe_mutation, doc_labels
from leaklab.replay.redact import assert_clean
from leaklab.replay.summary import (
    SummaryMismatch,
    UnmappableStatus,
    assert_equal,
    from_report,
    recompute,
    to_schema,
)
from leaklab.runner import load_events
from leaklab.runner.scenarios import ScenarioPlan, plan_from_dict
from leaklab.spec import TenantSpec

EXPORTER_VERSION = "1.0.0"
ROOT = Path(__file__).resolve().parents[3]
SCHEMA_PATH = ROOT / "docs" / "schemas" / "replay_bundle.schema.json"
ROLE_ORDER = {"flipped": 0, "control": 1, "other": 2}


class ExportError(Exception):
    """The journals cannot be exported faithfully (missing generation, wrong spec, ...)."""


@dataclass
class Bundle:
    name: str  # file name stem: <run_id>__<scenario>__r<n>
    data: dict[str, Any]


@dataclass
class Skipped:
    run_id: str
    scenario: str
    reason: str


@dataclass
class ExportResult:
    bundles: list[Bundle] = field(default_factory=list)
    skipped: list[Skipped] = field(default_factory=list)


def load_schema(path: Path = SCHEMA_PATH) -> dict[str, Any]:
    schema: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(schema)
    return schema


def git_commit() -> str:
    """Short commit id of the working tree, with -dirty if it has uncommitted changes."""
    try:
        head = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            cwd=ROOT, capture_output=True, text=True, check=True,
        ).stdout.strip()  # fmt: skip
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return f"{head}-dirty" if dirty else head


class Expected:
    """The oracle's intent per (persona, canary) at any time, cached per stretch of constant intent."""

    def __init__(self, timeline: IntentTimeline) -> None:
        self._timeline = timeline
        self._changes = sorted(timeline.change_times())
        self._cache: dict[int, Mapping[str, set[str]]] = {}

    def allowed(self, persona: str, canary: str, t: float) -> bool:
        epoch = bisect_right(self._changes, t)  # changes at or before t are in force
        if epoch not in self._cache:
            at = float("-inf") if epoch == 0 else self._changes[epoch - 1]
            self._cache[epoch] = self._timeline.expected_at(at)
        return canary in self._cache[epoch].get(persona, set())


def _outcome(obs: Mapping[str, Any]) -> str:
    if obs["served"] is True:
        return "served"
    if obs["served"] is False:
        return "denied"
    return "not_indexed" if obs.get("error") == "not_indexed" else "error"


def _verdict(outcome: str, expected: str) -> str:
    if outcome in ("error", "not_indexed"):
        return "unscored"  # no answer is never counted as a denial
    if outcome == "served":
        return "ok" if expected == "allow" else "leak"
    return "ok" if expected == "deny" else "lockout"


def _replicate(journal: Path, run_id: str, manifest: Mapping[str, Any] | None) -> int:
    if manifest:
        for entry in manifest.get("runs", []):
            if entry.get("journal") == journal.name:
                return int(entry["index"])
    match = re.search(r"_r(\d+)$", run_id)
    if not match:
        raise ExportError(
            f"cannot tell the replicate number of {run_id!r} (no manifest entry, no _rN suffix)"
        )
    return int(match.group(1))


def export_journal(
    journal: Path,
    report_file: Path,
    spec: TenantSpec,
    *,
    spec_sha256: str,
    replicate: int | None = None,
    manifest: Mapping[str, Any] | None = None,
    schema: dict[str, Any] | None = None,
    commit: str | None = None,
    exported_at: float | None = None,
) -> ExportResult:
    """All bundles of one run journal. Raises SummaryMismatch / RedactionError / ValidationError."""
    schema = schema or load_schema()
    commit = commit if commit is not None else git_commit()
    exported_at = exported_at if exported_at is not None else time.time()
    events = load_events(journal)
    header = next((e for e in events if e["type"] == "run_started"), None)
    if header is None:
        raise ExportError(f"{journal.name} has no run_started event")
    if header.get("spec_sha256") != spec_sha256:
        raise ExportError(
            f"{journal.name} was recorded with another tenant_spec.yaml; the oracle intent cannot be reproduced"
        )
    generation = header.get("generation")
    if not generation:
        raise ExportError(
            f"{journal.name} names no tenant generation: its canaries cannot be reconstructed"
        )
    report = json.loads(report_file.read_text(encoding="utf-8"))
    run_id = str(header["run_id"])
    if report["run_id"] != run_id:
        raise ExportError(f"{report_file.name} belongs to {report['run_id']}, not to {run_id}")
    rep = replicate if replicate is not None else _replicate(journal, run_id, manifest)
    spec_g = spec.with_generation(str(generation))
    config = header["config"]
    systems = [str(s) for s in header["systems"]]
    labels = doc_labels(spec_g)

    entries: dict[str, list[LogEntry]] = {}
    applied: list[Mutation] = []
    plans: dict[str, ScenarioPlan] = {}
    group_at: dict[str, float] = {}
    baseline: dict[str, Mapping[str, Any]] = {}
    done: dict[str, Mapping[str, Any]] = {}
    obs_by_pair: dict[tuple[str, str, str], list[Mapping[str, Any]]] = {}
    for e in events:
        kind = e["type"]
        if kind == "group_started":
            for raw in e["scenarios"]:
                plans[str(raw["id"])] = plan_from_dict(spec_g, raw)
                group_at[str(raw["id"])] = float(e["t"])
        elif kind == "mutation":
            entry = LogEntry.model_validate(e["entry"])
            entries.setdefault(str(e["scenario"]), []).append(entry)
            if entry.status != "failed":
                applied.append(
                    entry.mutation.model_copy(update={"confirmed_at": entry.confirmed_at})
                )
        elif kind == "baseline_done":
            baseline[str(e["scenario"])] = e
        elif kind == "scenario_done":
            done[str(e["scenario"])] = e
        elif kind == "obs":
            obs_by_pair.setdefault((e["system"], e["persona"], e["canary"]), []).append(e)
    timeline = IntentTimeline(spec_g, applied)
    expected = Expected(timeline)
    ready = next(
        (
            e
            for e in events
            if e["type"] == "system_event" and e.get("kind") == "setup_b_ready" and e.get("ready")
        ),
        None,
    )
    personas = [{"id": u.id, "display": u.display} for u in spec.users if u.persona]
    run_block = {
        "run_id": run_id,
        "generation": str(generation),
        "started_at": float(header["t"]),
        "read_interval_s": float(config["interval_s"]),
        "poll_cap_s": float(config["cap_s"]),
        "n_consecutive": int(config["n_consecutive"]),
        "systems": [{"id": s, **SYSTEMS[s]} for s in systems],
        "personas": personas,
    }
    result = ExportResult()
    for sid, plan in plans.items():
        mine = entries.get(sid, [])
        confirmed = [m.confirmed_at for m in mine if m.confirmed_at is not None]
        if not confirmed:
            result.skipped.append(
                Skipped(run_id, sid, "its mutation was never confirmed: there is no t=0")
            )
            continue
        t_change = max(confirmed)
        roles = {p: "revoking" for p in plan.revoking} | {p: "granting" for p in plan.granting}
        roles |= {p: "targeted" for p in plan.targeted if p not in roles}
        if plan.control and plan.control not in roles:
            roles[plan.control] = "control"
        bundle_role = {
            p: (
                "flipped"
                if r in ("revoking", "granting")
                else "control"
                if r == "control"
                else "other"
            )
            for p, r in roles.items()
        }
        reads: list[dict[str, Any]] = []
        for system in systems:
            for (persona, canary), _role in roles.items():
                for o in obs_by_pair.get((system, persona, canary), []):
                    t = float(o["ts"])
                    outcome = _outcome(o)
                    exp = "allow" if expected.allowed(persona, canary, t) else "deny"
                    status = (
                        o.get("final_status")
                        if o.get("final_status") is not None
                        else o.get("status")
                    )
                    reads.append(
                        {
                            "type": "read",
                            "t": t,
                            "t_rel_s": round(t - t_change, 3),
                            "phase": "baseline" if t < t_change else "poll",
                            "system": system,
                            "persona": persona,
                            "canary": canary,
                            "outcome": outcome,
                            "status_code": int(status) if status is not None else None,
                            "error_kind": o.get("error"),
                            "expected": exp,
                            "verdict": _verdict(outcome, exp),
                        }
                    )
        if not reads:
            result.skipped.append(Skipped(run_id, sid, "no reads of its pairs in the journal"))
            continue
        # -- summary: copied from the report, recomputed from THIS bundle's reads, asserted equal
        copied = from_report(report["results"], sid, systems)
        # the recomputation sees ONLY this scenario's own mutation: it must not need the others
        own = IntentTimeline(
            spec_g,
            [
                e.mutation.model_copy(update={"confirmed_at": e.confirmed_at})
                for e in mine
                if e.status != "failed"
            ],
        )
        recomputed = recompute(
            reads,
            roles,
            own,
            t_change,
            systems,
            int(config["n_consecutive"]),
            float(config["cap_s"]),
        )
        assert_equal(copied, recomputed, f"{run_id}/{sid}")
        try:
            summary = to_schema(copied)
        except UnmappableStatus as exc:
            result.skipped.append(Skipped(run_id, sid, str(exc)))
            continue
        window = (min(r["t"] for r in reads), max(r["t"] for r in reads))
        events_out: list[dict[str, Any]] = list(reads)
        mutation_event = {
            "type": "mutation",
            "t": t_change,
            "t_rel_s": 0.0,
            "t_requested": min(m.requested_at for m in mine),
            "t_confirmed": t_change,
            "mutation_type": mine[-1].mutation.type.value,
            "summary": "; ".join(describe_mutation(spec_g, m.mutation) for m in mine),
        }
        events_out.append(mutation_event)
        events_out += _phases(
            events, sid, t_change, group_at, baseline, done, ready, copied, config
        )
        events_out += _indexer_runs(events, systems, window, t_change)
        events_out += _poller_lag(events, window, t_change)
        events_out.sort(key=lambda e: (e["t"], {"mutation": 0, "phase": 1}.get(e["type"], 2)))
        data = {
            "schema_version": "1.0",
            "run": run_block,
            "scenario": {
                "id": sid,
                "description": next(s.description for s in spec.scenarios if s.id == sid),
                "replicate": rep,
                "baseline_target_s": round(
                    int(config["baseline_k"]) * float(config["interval_s"]), 3
                ),
                "tracked_pairs": [
                    {"persona": p, "canary": c, "doc_label": labels[c], "role": bundle_role[(p, c)]}
                    for (p, c) in sorted(roles, key=lambda pc: (ROLE_ORDER[bundle_role[pc]], pc))
                ],
            },
            "events": events_out,
            "summary": summary,
            "provenance": {
                "journal_file": journal.name,
                "report_file": report_file.name,
                "exporter_version": EXPORTER_VERSION,
                "git_commit": commit,
                "exported_at": exported_at,
            },
        }
        jsonschema.Draft202012Validator(schema).validate(data)
        assert_clean(data, f"{run_id}/{sid}")
        result.bundles.append(Bundle(f"{run_id}__{sid}__r{rep}", data))
    return result


def _phases(
    events: Sequence[Mapping[str, Any]],
    sid: str,
    t_change: float,
    group_at: Mapping[str, float],
    baseline: Mapping[str, Mapping[str, Any]],
    done: Mapping[str, Mapping[str, Any]],
    ready: Mapping[str, Any] | None,
    copied: Sequence[Mapping[str, Any]],
    config: Mapping[str, Any],
) -> list[dict[str, Any]]:
    def ph(
        name: str, t: float, system: str | None = None, detail: str | None = None
    ) -> dict[str, Any]:
        return {
            "type": "phase",
            "t": t,
            "t_rel_s": round(t - t_change, 3),
            "name": name,
            "system": system,
            "detail": detail,
        }

    out: list[dict[str, Any]] = []
    if ready is not None and ready.get("t") is not None:  # no time, no event: nothing is made up
        out.append(
            ph(
                "ready",
                float(ready["t"]),
                "setup_b",
                f"ready {ready['time_to_ready_s']:.1f} s after the build started",
            )
        )
    if sid in group_at:
        out.append(ph("baseline_start", group_at[sid]))
    if sid in baseline:
        b = baseline[sid]
        out.append(
            ph(
                "baseline_stable",
                float(b["t"]),
                None,
                f"{b['status']}, {b['min_streak']} conforming reads in a row",
            )
        )
    out.append(ph("poll_start", t_change))
    end = done.get(sid)
    for c in copied:
        system = str(c["system"])
        if c["first_deny_s"] is not None:
            out.append(ph("first_deny", t_change + float(c["first_deny_s"]), system))
        if c["convergence"] == "CONFORMED" and c["time_to_conformance_s"] is not None:
            out.append(ph("converged", t_change + float(c["time_to_conformance_s"]), system))
        elif c["convergence"] == "DID_NOT_CONVERGE":
            when = (
                float(end["t"])
                if end is not None and end.get("reason") == "cap"
                else t_change + float(config["cap_s"])
            )
            out.append(ph("cap_reached", when, system))
    return out


def _indexer_runs(
    events: Sequence[Mapping[str, Any]],
    systems: Sequence[str],
    window: tuple[float, float],
    t_change: float,
) -> list[dict[str, Any]]:
    finished: dict[tuple[str, float], Mapping[str, Any]] = {}
    started: set[tuple[str, float]] = set()
    for e in events:
        if e["type"] != "system_event" or e["system"] not in INDEX_OF or e["system"] not in systems:
            continue
        key = (str(e["system"]), float(e["start"])) if "start" in e else None
        if key is None:
            continue
        if e.get("kind") == "indexer_run":
            finished[key] = e
        elif e.get("kind") == "indexer_run_started":
            started.add(key)
    out: list[dict[str, Any]] = []
    for system, start in sorted(started | set(finished)):
        fin = finished.get((system, start))
        end = float(fin["end"]) if fin is not None and fin.get("end") is not None else None
        if (end if end is not None else start) < window[0] or start > window[1]:
            continue
        out.append(
            {
                "type": "indexer_run",
                "t": start,
                "t_rel_s": round(start - t_change, 3),
                "system": system,
                "index": INDEX_OF[system],
                "t_start": start,
                "t_end": end,
                "status": str(fin["status"]) if fin is not None else "unfinished",
                "items_processed": int(e["items_processed"])
                if e is not None and e.get("items_processed") is not None
                else None,
            }
        )
    return out


def _poller_lag(
    events: Sequence[Mapping[str, Any]], window: tuple[float, float], t_change: float
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for e in events:
        if e["type"] != "system_event" or e.get("kind") != "poller_lag":
            continue
        if e.get("t") is None:
            continue  # no time, no event: nothing is made up
        t = float(e["t"])
        if not window[0] <= t <= window[1]:
            continue
        for poller, key in (("membership", "membership_age_s"), ("acl", "acl_age_s")):
            if e.get(key) is not None:
                out.append(
                    {
                        "type": "poller_lag",
                        "t": t,
                        "t_rel_s": round(t - t_change, 3),
                        "poller": poller,
                        "lag_s": max(0.0, float(e[key])),
                    }
                )
    return out


def export_folder(runs: Path, spec: TenantSpec, spec_sha256: str) -> ExportResult:
    """Every run journal of a benchmark folder (run_*.jsonl with its .report.json)."""
    manifest_path = runs / "manifest.json"
    manifest = (
        json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else None
    )
    schema = load_schema()
    commit = git_commit()
    now = time.time()
    total = ExportResult()
    for journal in sorted(runs.glob("run_*_r*.jsonl")):
        report_file = journal.with_suffix(".report.json")
        if not report_file.exists():
            total.skipped.append(
                Skipped(journal.stem, "*", f"{report_file.name} does not exist (run not finished?)")
            )
            continue
        one = export_journal(
            journal,
            report_file,
            spec,
            spec_sha256=spec_sha256,
            manifest=manifest,
            schema=schema,
            commit=commit,
            exported_at=now,
        )
        total.bundles += one.bundles
        total.skipped += one.skipped
    return total


def index_entry(b: Bundle) -> dict[str, Any]:
    d = b.data
    return {
        "bundle": f"{b.name}.json",
        "run_id": d["run"]["run_id"],
        "scenario": d["scenario"]["id"],
        "replicate": d["scenario"]["replicate"],
        "systems": [s["id"] for s in d["run"]["systems"]],
        "summary": [{"system": s["system"], "status": s["status"]} for s in d["summary"]],
    }


def write_bundles(result: ExportResult, out: Path) -> list[Path]:
    """<out>/<run_id>__<scenario>__r<n>.json for every bundle, plus <out>/index.json."""
    out.mkdir(parents=True, exist_ok=True)
    paths = []
    for b in result.bundles:
        path = out / f"{b.name}.json"
        path.write_text(json.dumps(b.data, indent=1) + "\n", encoding="utf-8")
        paths.append(path)
    index = {"schema_version": "1.0", "bundles": [index_entry(b) for b in result.bundles]}
    assert_clean(index, "index.json")
    (out / "index.json").write_text(json.dumps(index, indent=1) + "\n", encoding="utf-8")
    return paths


__all__ = [
    "Bundle",
    "ExportError",
    "ExportResult",
    "Skipped",
    "SummaryMismatch",
    "export_folder",
    "export_journal",
    "write_bundles",
]
