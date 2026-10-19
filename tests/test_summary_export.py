"""summary.json: built from a report.json and the S6 bundles, valid against its schema, nothing typed."""

import json
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from leaklab.replay.redact import RedactionError
from leaklab.replay.summary_export import SUMMARY_SCHEMA, build_summary, write_summary
from runner_fakes import SPEC


def stat(n: int, v: float | None) -> dict[str, Any]:
    return {"n": n, "min": v, "median": v, "max": v}


def cell(scenario: str, system: str, v: float | None, **kw: Any) -> dict[str, Any]:
    base = {
        "scenario": scenario, "system": system, "planned": 1, "replicates": 1, "first_deny": stat(0 if v is None else 1, v),
        "leak_exposure": stat(1, 0.0), "did_not_converge": 0, "nothing_to_revoke": 0, "observations": 100,
    }  # fmt: skip
    return {**base, **kw}


def s6_cell(variant: str, arm: str, leaks: int, trials: int = 3, **kw: Any) -> dict[str, Any]:
    return {
        "variant": variant,
        "arm": arm,
        "trials": trials,
        "leak_trials": leaks,
        "invalid_trials": 0,
        "verdicts": {},
        **kw,
    }


def report() -> dict[str, Any]:
    return {
        "config": {"interval_s": 5.0, "cap_s": 3600.0, "n_consecutive": 10},
        "cells": [cell("S1-entra", "setup0", 2700.0), cell("S1-entra", "setup_b", 10.0, replicates=2), cell("S1-spgroup", "setup_a_basic", None, nothing_to_revoke=1)],
        "runs": [{"finished": True}, {"finished": False}],
        "caveats": ["a caveat"],
        "s6": {
            "cells": [s6_cell("S6a", "agent_naive_a", 3), s6_cell("S6a", "agent_naive_b", 2), s6_cell("S6c", "agent_naive_a", 3),
                      s6_cell("S6a", "agent_guarded_b", 0), s6_cell("control", "agent_naive_a", 0, verdicts={"SERVED": 3})],
        },
    }  # fmt: skip


def bundle(scenario: str, mode: str, verdict: str, paths: list[tuple[str, str]]) -> dict[str, Any]:
    return {
        "trial": {"scenario": scenario, "arm": {"mode": mode}},
        "summary": {"verdict": verdict},
        "turns": [{"path": p, "verdict": v} for p, v in paths],
    }


BUNDLES = [
    bundle("S6a", "naive", "LEAK", [("cache_hit", "leak")]),
    bundle("S6a", "naive", "LEAK", [("cache_hit", "leak")]),
    bundle("S6c", "naive", "LEAK", [("memory_hit", "leak")]),
    bundle("S6a", "naive", "LEAK", [("retrieval", "leak")]),
    bundle("S6a", "guarded", "OK", [("retrieval", "ok")]),
]


def test_the_summary_validates_and_copies_the_cells() -> None:
    s = build_summary(report(), BUNDLES, SPEC)
    jsonschema.Draft202012Validator(
        json.loads(SUMMARY_SCHEMA.read_text(encoding="utf-8"))
    ).validate(s)
    entra = next(c for c in s["cells"] if c["scenario"] == "S1-entra" and c["system"] == "setup0")
    assert entra["first_deny"]["median"] == 2700.0 and entra["replicates"] == 1
    spg = next(c for c in s["cells"] if c["scenario"] == "S1-spgroup")
    assert spg["nothing_to_revoke"] == 1 and spg["first_deny"]["median"] is None


def test_totals_come_from_the_report_and_the_spec() -> None:
    t = build_summary(report(), BUNDLES, SPEC)["totals"]
    assert t["runs"] == 1  # only finished runs
    assert t["replicates"] == 3  # S1-entra: max over its systems (2) + S1-spgroup (1)
    assert t["reads"] == 300
    assert t["documents"] == 45 and t["personas"] == 7


def test_s6_sums_and_leak_origin() -> None:
    s6 = build_summary(report(), BUNDLES, SPEC)["s6"]
    assert (s6["naive"]["trials"], s6["naive"]["leaks"]) == (9, 8)
    assert (s6["naive"]["leaks_via_cache_memory"], s6["naive"]["leaks_via_retrieval"]) == (3, 1)
    assert (s6["guarded"]["trials"], s6["guarded"]["leaks"]) == (3, 0)
    assert s6["positive_control"] == {"trials": 3, "served": 3}


def test_a_report_without_s6_gives_null() -> None:
    r = report()
    del r["s6"]
    assert build_summary(r, [], SPEC)["s6"] is None


def test_write_summary_writes_summary_and_a_clean_report_copy(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    runs.mkdir()
    (runs / "report.json").write_text(json.dumps(report()), encoding="utf-8")
    written = write_summary(runs, tmp_path / "out", SPEC, BUNDLES)
    assert [p.name for p in written] == ["summary.json", "report.json"]
    assert json.loads((tmp_path / "out" / "report.json").read_text(encoding="utf-8"))[
        "caveats"
    ] == ["a caveat"]


def test_nothing_is_written_without_a_report(tmp_path: Path) -> None:
    assert write_summary(tmp_path, tmp_path / "out", SPEC, []) == []


def test_a_report_with_an_identifier_is_refused(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    runs.mkdir()
    bad = report()
    bad["caveats"] = ["someone@contoso.onmicrosoft.com"]
    (runs / "report.json").write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(RedactionError):
        write_summary(runs, tmp_path / "out", SPEC, [])
