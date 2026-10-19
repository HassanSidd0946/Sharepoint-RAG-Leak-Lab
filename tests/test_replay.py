"""Tests of the replay exporter and figures, over a fake journal (no tenant, no network)."""

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from benchmark_fakes import FakeRuns
from leaklab.replay.cli import app
from leaklab.replay.export import (
    ExportResult,
    export_folder,
    export_journal,
    load_schema,
    write_bundles,
)
from leaklab.replay.figures import (
    best_demo_bundle,
    first_deny_figure,
    load_bundles,
    timeline_figure,
)
from leaklab.replay.redact import find_leaks
from leaklab.replay.summary import SummaryMismatch
from leaklab.runner import RunConfig
from leaklab.runner.run import spec_sha256
from runner_fakes import SPEC, SPEC_PATH

SHA = spec_sha256(SPEC_PATH)
CONFIG = RunConfig(baseline_k=3, baseline_cap_s=300.0, n_consecutive=4, cap_s=900.0, interval_s=5.0)
SYSTEMS = ["setup0", "setup_a_basic", "setup_b"]


def make_folder(tmp_path: Path, **kw: Any) -> Path:
    runs = FakeRuns(
        {"setup0": {"lag_s": 100.0}, "setup_a_basic": {"lag_s": 40.0}, **kw},
    )
    folder = tmp_path / "runs"
    folder.mkdir()
    for suffix in ("_r1", "_r2"):
        runs(
            spec_path=SPEC_PATH,
            scenario_ids=["S1-direct"],
            system_names=SYSTEMS,
            config=CONFIG,
            out_dir=folder,
            suffix=suffix,
        )
    return folder


@pytest.fixture(scope="module")
def exported(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, ExportResult]:
    folder = make_folder(tmp_path_factory.mktemp("fake"))
    result = export_folder(folder, SPEC, SHA)
    return folder, result


def reads(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    return [e for e in bundle["events"] if e["type"] == "read"]


def test_every_bundle_is_schema_valid_and_there_is_one_per_run(exported) -> None:  # type: ignore[no-untyped-def]
    _, result = exported
    assert not result.skipped
    assert len(result.bundles) == 2
    schema = load_schema()
    import jsonschema

    for b in result.bundles:
        jsonschema.Draft202012Validator(schema).validate(b.data)
    assert sorted(b.data["scenario"]["replicate"] for b in result.bundles) == [1, 2]


def test_verdicts_come_from_the_oracle_not_from_the_system(exported) -> None:  # type: ignore[no-untyped-def]
    _, result = exported
    data = result.bundles[0].data
    rs = reads(data)
    # before the change everything the oracle allows is served: ok; after it, the lagging systems leak
    assert all(r["t_rel_s"] >= 0 for r in rs if r["phase"] == "poll")
    assert all(r["t_rel_s"] < 0 for r in rs if r["phase"] == "baseline")
    leaks = [r for r in rs if r["verdict"] == "leak"]
    assert leaks and all(r["expected"] == "deny" and r["outcome"] == "served" for r in leaks)
    assert {r["system"] for r in leaks} == {"setup0", "setup_a_basic"}
    assert not any(r["verdict"] == "leak" for r in rs if r["system"] == "setup_b")
    for r in rs:
        if r["verdict"] == "ok":
            assert (r["outcome"] == "served") == (r["expected"] == "allow")


def test_errors_are_unscored_never_denied(tmp_path: Path) -> None:
    folder = make_folder(tmp_path, setup_b={"error_every": 5})
    result = export_folder(folder, SPEC, SHA)
    rs = [r for b in result.bundles for r in reads(b.data) if r["system"] == "setup_b"]
    errs = [r for r in rs if r["outcome"] in ("error", "not_indexed")]
    assert errs and all(r["verdict"] == "unscored" for r in errs)
    assert all(r["verdict"] in ("ok", "leak", "lockout", "unscored") for r in rs)


def test_summary_is_copied_and_equals_the_recomputation(exported) -> None:  # type: ignore[no-untyped-def]
    _, result = exported
    by_system = {s["system"]: s for s in result.bundles[0].data["summary"]}
    assert set(by_system) == set(SYSTEMS)
    assert (
        by_system["setup0"]["first_deny_s"]
        > by_system["setup_a_basic"]["first_deny_s"]
        > by_system["setup_b"]["first_deny_s"]
        > 0
    )
    assert all(s["status"] == "CONVERGED" for s in by_system.values())


def _bump(obj: Any) -> bool:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "time_to_first_deny_s" and isinstance(v, (int, float)):
                obj[k] = v + 5.0
                return True
            if _bump(v):
                return True
    elif isinstance(obj, list):
        return any(_bump(v) for v in obj)
    return False


def test_a_planted_summary_mismatch_fails_the_export_loudly(tmp_path: Path) -> None:
    folder = make_folder(tmp_path)
    report_file = next(folder.glob("run_*_r1.report.json"))
    report = json.loads(report_file.read_text(encoding="utf-8"))
    assert _bump(report)
    report_file.write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(SummaryMismatch):
        export_folder(folder, SPEC, SHA)


def test_a_journal_of_another_spec_is_refused(tmp_path: Path) -> None:
    from leaklab.replay.export import ExportError

    folder = make_folder(tmp_path)
    with pytest.raises(ExportError, match="another tenant_spec"):
        export_folder(folder, SPEC, "0" * 64)


def test_redaction_scanner_finds_each_kind_of_identifier() -> None:
    bad = {
        "a": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
        "b": "alice@contoso.com",
        "c": "x.sharepoint.com",
        "d": "t.onmicrosoft.com",
        "e": "https://example.org/x",
        "f": ["01ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"],
    }
    found = find_leaks(bad)
    assert len(found) >= 6
    assert find_leaks({"ok": "Finance/ll-fin-01.txt", "p": "p-ceo"}) == []


def test_every_exported_bundle_is_free_of_identifiers(exported, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    _, result = exported
    paths = write_bundles(result, tmp_path / "out")
    assert len(paths) == 2
    for p in [*paths, tmp_path / "out" / "index.json"]:
        text = p.read_text(encoding="utf-8")
        for needle in ("@", "sharepoint.com", "onmicrosoft", "http://", "https://"):
            assert needle not in text.lower(), (p.name, needle)
        assert find_leaks(json.loads(text)) == []


def test_index_lists_every_bundle_with_statuses(exported, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    _, result = exported
    out = tmp_path / "out"
    write_bundles(result, out)
    index = json.loads((out / "index.json").read_text(encoding="utf-8"))
    assert len(index["bundles"]) == 2
    entry = index["bundles"][0]
    assert set(entry) == {"bundle", "run_id", "scenario", "replicate", "systems", "summary"}
    assert (out / entry["bundle"]).exists()


def test_exporting_one_journal_directly(exported) -> None:  # type: ignore[no-untyped-def]
    folder, _ = exported
    journal = sorted(folder.glob("run_*_r1.jsonl"))[0]
    one = export_journal(journal, journal.with_suffix(".report.json"), SPEC, spec_sha256=SHA)
    assert one.bundles[0].data["scenario"]["replicate"] == 1
    prov = one.bundles[0].data["provenance"]
    assert prov["journal_file"] == journal.name and prov["exporter_version"]


def test_figures_build_from_the_bundles(exported, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    _, result = exported
    out = tmp_path / "out"
    write_bundles(result, out)
    bundles = load_bundles(out)
    assert len(bundles) == 2
    chosen = best_demo_bundle(bundles, scenario="S1-direct")
    assert chosen is not None
    for axis in ("symlog", "linear"):
        files = timeline_figure(chosen, bundles, tmp_path / f"tl_{axis}", axis)
        assert {Path(f).suffix for f in files} == {".svg", ".png"}
        assert all(Path(f).stat().st_size > 1000 for f in files)
    files = first_deny_figure(bundles, tmp_path / "dots")
    assert {Path(f).suffix for f in files} == {".svg", ".png"}
    svg = (tmp_path / "dots.svg").read_text(encoding="utf-8")
    assert "n = 2" in svg  # the caption states how many replicates are drawn
    assert "Azure AI Search SharePoint indexer" in svg  # and the configuration caveat


def test_did_not_converge_is_drawn_at_the_cap(tmp_path: Path) -> None:
    folder = make_folder(tmp_path, setup_a_basic={"lag_s": 0.0, "never_revoke": []})
    result = export_folder(folder, SPEC, SHA)
    for b in result.bundles:
        b.data["summary"][1] |= {"status": "DID_NOT_CONVERGE", "first_deny_s": None}
    out = tmp_path / "out"
    write_bundles(result, out)
    files = first_deny_figure(load_bundles(out), tmp_path / "dnc")
    assert all(Path(f).exists() for f in files)


def test_cli_export_and_figures(exported, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    folder, _ = exported
    runner = CliRunner()
    out = tmp_path / "cli"
    res = runner.invoke(
        app, ["export", "--runs", str(folder), "--out", str(out), "--spec", str(SPEC_PATH)]
    )
    assert res.exit_code == 0, res.output
    assert (out / "index.json").exists()
    figs = tmp_path / "figs"
    res = runner.invoke(
        app, ["figures", "--bundles", str(out), "--out", str(figs), "--axis", "linear"]
    )
    # the fake scenario is S1-direct: no S1-entra bundle, so the timeline needs --bundle
    assert res.exit_code == 2
    name = json.loads((out / "index.json").read_text(encoding="utf-8"))["bundles"][0]["bundle"]
    res = runner.invoke(
        app,
        ["figures", "--bundles", str(out), "--out", str(figs), "--bundle", name[:-5],
         "--axis", "linear"],
    )  # fmt: skip
    assert res.exit_code == 0, res.output
    assert (figs / "first_deny_dotplot.png").exists()
