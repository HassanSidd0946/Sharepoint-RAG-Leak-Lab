"""report.json (+ the S6 bundles) -> public/summary.json: the numbers the Findings page shows.

Everything here is copied or counted from report.json and the exported bundles; nothing is typed. The
file is checked against docs/schemas/summary.schema.json and the redaction scanner before it is written.
report.json itself is also copied next to it (the Results page reads it); it passes the same scanner.

WHERE EACH FIELD COMES FROM
  cells[]      report.json cells (planned, replicates, first_deny, leak_exposure, did_not_converge,
               nothing_to_revoke): the median, min, max and n of the runner's own report
  s6           report.json s6.cells (leak_trials, trials, SERVED) summed per arm mode; leaks_via_* from
               the agent_trial bundles (the path of the turn that delivered the leaked canary)
  totals       runs finished, replicates (complete (scenario, run) pairs), reads (report observations),
               documents and personas (tenant_spec.yaml)
  caveats      report.json caveats, unchanged
"""

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import jsonschema

from leaklab.generator.plan import build_plan
from leaklab.replay.labels import SYSTEMS
from leaklab.replay.redact import assert_clean
from leaklab.spec import TenantSpec

ROOT = Path(__file__).resolve().parents[3]
SUMMARY_SCHEMA = ROOT / "docs" / "schemas" / "summary.schema.json"
SUMMARY_VERSION = "1.0"
NAIVE_ARMS = ("agent_naive_a", "agent_naive_b")
GUARDED_ARMS = ("agent_guarded_b",)


def _stat(raw: Mapping[str, Any]) -> dict[str, Any]:
    return {k: raw[k] for k in ("n", "min", "median", "max")}


def _cell(c: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "scenario": c["scenario"],
        "system": c["system"],
        "planned": int(c["planned"]),
        "replicates": int(c["replicates"]),
        "first_deny": _stat(c["first_deny"]),
        "leak_exposure": _stat(c["leak_exposure"]),
        "did_not_converge": int(c["did_not_converge"]),
        "nothing_to_revoke": int(c["nothing_to_revoke"]),
    }


def _s6_group(
    s6: Mapping[str, Any], arms: Sequence[str], bundles: Sequence[Mapping[str, Any]], mode: str
) -> dict[str, int]:
    attack = [c for c in s6["cells"] if c["variant"] in ("S6a", "S6b", "S6c") and c["arm"] in arms]
    via_store = via_retrieval = 0
    for b in bundles:
        if b["trial"]["scenario"] not in ("S6a", "S6b", "S6c") or b["trial"]["arm"]["mode"] != mode:
            continue
        if b["summary"]["verdict"] != "LEAK":
            continue
        paths = {t["path"] for t in b["turns"] if t["verdict"] == "leak"}
        if "retrieval" in paths:
            via_retrieval += 1
        else:
            via_store += 1
    return {
        "trials": sum(int(c["trials"]) for c in attack),
        "leaks": sum(int(c["leak_trials"]) for c in attack),
        "invalid": sum(int(c["invalid_trials"]) for c in attack),
        "leaks_via_cache_memory": via_store,
        "leaks_via_retrieval": via_retrieval,
    }


def build_summary(
    report: Mapping[str, Any], agent_bundles: Sequence[Mapping[str, Any]], spec: TenantSpec
) -> dict[str, Any]:
    cells = [_cell(c) for c in report["cells"]]
    by_scenario: dict[str, int] = {}
    for c in cells:
        by_scenario[c["scenario"]] = max(by_scenario.get(c["scenario"], 0), c["replicates"])
    s6 = report.get("s6")
    plan = build_plan(spec)
    out: dict[str, Any] = {
        "schema_version": SUMMARY_VERSION,
        "systems": [
            {"id": k, "label": v["label"], "config_label": v["config_label"]}
            for k, v in SYSTEMS.items()
        ],
        "config": {
            "read_interval_s": report["config"]["interval_s"],
            "poll_cap_s": report["config"]["cap_s"],
            "n_consecutive": report["config"]["n_consecutive"],
        },
        "cells": cells,
        "s6": None
        if not s6
        else {
            "naive": _s6_group(s6, NAIVE_ARMS, agent_bundles, "naive"),
            "guarded": _s6_group(s6, GUARDED_ARMS, agent_bundles, "guarded"),
            "positive_control": {
                "trials": sum(int(c["trials"]) for c in s6["cells"] if c["variant"] == "control"),
                "served": sum(
                    int(c["verdicts"].get("SERVED", 0))
                    for c in s6["cells"]
                    if c["variant"] == "control"
                ),
            },
        },
        "totals": {
            "runs": sum(1 for r in report["runs"] if r.get("finished")),
            "replicates": sum(by_scenario.values()),
            "reads": sum(int(c["observations"]) for c in report["cells"]),
            "documents": sum(len(f.docs) for f in plan.folders) + len(plan.special_files),
            "personas": sum(1 for u in spec.users if u.persona),
        },
        "caveats": [str(c) for c in report["caveats"]],
    }
    return out


def write_summary(
    runs: Path, out: Path, spec: TenantSpec, agent_bundles: Sequence[Mapping[str, Any]]
) -> list[Path]:
    """summary.json and a copy of report.json in `out`, if `runs` has a report.json. Returns what it wrote."""
    report_file = runs / "report.json"
    if not report_file.exists():
        return []
    report = json.loads(report_file.read_text(encoding="utf-8"))
    summary = build_summary(report, agent_bundles, spec)
    schema = json.loads(SUMMARY_SCHEMA.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator(schema).validate(summary)
    assert_clean(summary, "summary.json")
    assert_clean(report, "report.json")
    out.mkdir(parents=True, exist_ok=True)
    paths = [out / "summary.json", out / "report.json"]
    paths[0].write_text(json.dumps(summary, indent=1) + "\n", encoding="utf-8")
    paths[1].write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8")
    return paths
