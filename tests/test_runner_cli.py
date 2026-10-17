"""The runner's command line, without touching a tenant (run_once is replaced)."""

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import leaklab.runner.cli as cli
from leaklab.runner import RunnerError, build_report
from leaklab.runner.model import RunConfig
from leaklab.spec import load_spec
from runner_fakes import SPEC_PATH
from test_runner_metrics import full_journal

KNOWN = [s.id for s in load_spec(SPEC_PATH).scenarios]
runner = CliRunner()


def test_scenario_names_and_patterns_expand_in_spec_order_without_repeats() -> None:
    assert cli.expand_scenarios(["S1-direct"], KNOWN) == ["S1-direct"]
    assert cli.expand_scenarios(["S1-entra,S1-direct", "S1-direct"], KNOWN) == [
        "S1-entra",
        "S1-direct",
    ]
    assert cli.expand_scenarios(["S1-*"], KNOWN) == [
        "S1-parent-revocation",
        "S1-entra",
        "S1-spgroup",
        "S1-direct",
    ]


def test_an_unmatched_scenario_name_lists_what_exists() -> None:
    with pytest.raises(RunnerError, match="S4-move-into-restricted"):
        cli.expand_scenarios(["nope"], KNOWN)


def test_report_prints_the_table_for_a_journal(tmp_path: Path) -> None:
    journal = tmp_path / "run.jsonl"
    journal.write_text("\n".join(json.dumps(e) for e in full_journal()) + "\n", encoding="utf-8")
    result = runner.invoke(cli.app, ["report", "--run", str(journal)], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.output
    for text in ("S1-direct", "fake", "LEAK", "BASELINE_OK"):
        assert text in result.output


def test_report_as_json_is_the_same_data(tmp_path: Path) -> None:
    journal = tmp_path / "run.jsonl"
    journal.write_text("\n".join(json.dumps(e) for e in full_journal()) + "\n", encoding="utf-8")
    result = runner.invoke(cli.app, ["report", "--run", str(journal), "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    expected = build_report(load_spec(SPEC_PATH), full_journal()).model_dump()
    assert (
        data["results"][0]["time_to_first_deny_s"] == expected["results"][0]["time_to_first_deny_s"]
    )


def test_run_passes_the_options_through_and_makes_replicates_fresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []
    report = build_report(load_spec(SPEC_PATH), full_journal())

    def fake_run_once(**kwargs: Any) -> tuple[Path, Any]:
        calls.append(kwargs)
        return tmp_path / "run.jsonl", report

    monkeypatch.setattr(cli, "run_once", fake_run_once)
    result = runner.invoke(
        cli.app,
        [
            "run", "-s", "S1-spgroup", "-s", "S1-direct", "--systems", "setup0",
            "--replicate", "2", "--n", "7", "--interval-s", "3", "--out-dir", str(tmp_path),
        ],
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    assert [c["scenario_ids"] for c in calls] == [["S1-spgroup", "S1-direct"]] * 2
    assert [c["fresh"] for c in calls] == [False, True]  # replicate 2 starts from a clean tenant
    assert [c["suffix"] for c in calls] == ["_r1", "_r2"]
    config: RunConfig = calls[0]["config"]
    assert config.n_consecutive == 7 and config.interval_s == 3.0 and config.baseline_k == 5


def test_resume_cannot_be_combined_with_replicates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "run_once", lambda **kw: pytest.fail("must not run"))
    result = runner.invoke(
        cli.app,
        ["run", "-s", "S1-direct", "--resume", str(tmp_path / "x.jsonl"), "--replicate", "2"],
    )
    assert result.exit_code == 2 and "cannot be combined" in result.output


def test_an_unknown_scenario_is_a_clean_error_not_a_traceback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "run_once", lambda **kw: pytest.fail("must not run"))
    result = runner.invoke(cli.app, ["run", "-s", "nope"])
    assert result.exit_code == 2 and "no scenario matches" in result.output


def test_run_config_rejects_nonsense() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        RunConfig(baseline_k=0)
    with pytest.raises(ValueError, match="must be > 0"):
        RunConfig(cap_s=0)
