"""The benchmark: the matrix and plan, the resumable executor, the aggregate and the report.

The executor, aggregate and report are exercised end to end with real engine journals produced
over the fake tenant (benchmark_fakes.FakeRuns): fake time, no network.
"""

import json
import re
from pathlib import Path

import pytest

from benchmark_fakes import FakeRuns
from leaklab.benchmark.aggregate import SMALL_N, load_runs, stat, summarize
from leaklab.benchmark.execute import (
    MANIFEST,
    PLAN,
    init_manifest,
    load_manifest,
    load_plan,
    run_benchmark,
)
from leaklab.benchmark.matrix import (
    DEFAULT_ARMS,
    SYSTEMS,
    Arm,
    BenchmarkPlan,
    baseline_minutes,
    build_plan,
)
from leaklab.benchmark.render import HTML_NAME, JSON_NAME, render_html, write_report
from leaklab.runner import RunnerError
from leaklab.runner.metrics import SETUP_B_LABEL
from runner_fakes import SPEC, SPEC_PATH

SHA = "a" * 64


def plan(**kw: object) -> BenchmarkPlan:
    return build_plan(SPEC, SHA, **kw)  # type: ignore[arg-type]


# -- the matrix -----------------------------------------------------------------------------------


def test_the_default_matrix_has_the_asked_replicates_in_five_runs() -> None:
    p = plan()
    assert [r.index for r in p.runs] == [1, 2, 3, 4, 5]
    for arm in DEFAULT_ARMS:
        assert sum(arm.scenario in r.scenarios for r in p.runs) == arm.replicates
    assert {a.scenario: a.replicates for a in p.arms} == {
        "S1-direct": 3,
        "S1-spgroup": 3,
        "S1-entra": 5,
        "S2-nested-group-removal": 5,
        "S4-move-into-restricted": 2,
    }
    assert p.systems == list(SYSTEMS) == ["setup0", "setup_a_basic", "setup_b"]


def test_s1_entra_alternates_its_baseline_and_the_poll_settings_are_the_asked_ones() -> None:
    p = plan()
    entra = [r.baseline_minutes for r in p.runs if "S1-entra" in r.scenarios]
    assert entra == [1, 10, 1, 10, 1]
    for r in p.runs:
        assert r.config["interval_s"] == 5.0 and r.config["n_consecutive"] == 10
        assert r.config["cap_s"] == 3600.0
        assert r.config["baseline_k"] == r.baseline_minutes * 60 / 5.0  # reads covering the minutes


def test_s2_shares_a_polling_loop_with_s1_entra_and_every_run_is_one_loop() -> None:
    p = plan()
    for r in p.runs:
        assert len(r.loops) == 1, r.loops
        assert set(r.loops[0]) == set(r.scenarios)
    assert any("S1-entra" in r.loops[0] and "S2-nested-group-removal" in r.loops[0] for r in p.runs)
    assert "no scenario waits" in p.notes[0]


def test_scenarios_that_interfere_are_split_into_loops_and_the_plan_says_so() -> None:
    arms = (Arm("S1-parent-revocation", 1), Arm("S2-engineering-nested-removal", 1))
    p = plan(arms=arms)
    assert [len(r.loops) for r in p.runs] == [2]
    assert "one after the other" in p.notes[0]
    assert p.runs[0].max_polling_s > plan(arms=(Arm("S1-direct", 1),)).runs[0].max_polling_s


def test_the_plan_refuses_unknown_scenarios_and_zero_replicates() -> None:
    with pytest.raises(RunnerError, match="S99"):
        plan(arms=(Arm("S99", 1),))
    with pytest.raises(RunnerError, match="replicates"):
        plan(arms=(Arm("S1-direct", 0),))
    with pytest.raises(RunnerError, match="no mutations"):
        plan(arms=(Arm("S6-cached-answer", 1),))


def test_the_fingerprint_follows_the_plan() -> None:
    assert plan().sha256() == plan().sha256()
    assert plan().sha256() != plan(cap_s=1800.0).sha256()
    assert plan().sha256() != build_plan(SPEC, "b" * 64).sha256()


def test_the_baseline_helper_alternates_only_for_alternating_arms() -> None:
    arms = list(DEFAULT_ARMS)
    assert [baseline_minutes(arms, ["S1-entra"], i) for i in (1, 2, 3, 4)] == [1, 10, 1, 10]
    assert baseline_minutes(arms, ["S1-direct"], 2) == 1


# -- the executor ---------------------------------------------------------------------------------


SMALL = dict(arms=(Arm("S1-direct", 2), Arm("S1-entra", 3, baseline_alternates=True)), cap_s=900.0)


def test_every_run_is_fresh_uses_its_plan_and_lands_in_the_manifest(tmp_path: Path) -> None:
    p, runs = plan(**SMALL), FakeRuns()
    manifest = run_benchmark(p, tmp_path, spec_path=SPEC_PATH, run_fn=runs)
    assert [r.status for r in manifest.runs] == ["done"] * 3
    assert [c["fresh"] for c in runs.calls] == [True, True, True]
    assert [c["suffix"] for c in runs.calls] == ["_r1", "_r2", "_r3"]
    assert [c["scenarios"] for c in runs.calls] == [
        ["S1-direct", "S1-entra"],
        ["S1-direct", "S1-entra"],
        ["S1-entra"],
    ]
    assert [c["config"].baseline_k for c in runs.calls] == [12, 120, 12]
    assert all(c["resume"] is None for c in runs.calls)
    for entry in manifest.runs:
        assert entry.journal and (tmp_path / entry.journal).exists()
        assert entry.journal.endswith(f"_{entry.name}.jsonl")
    assert (tmp_path / PLAN).exists() and load_plan(tmp_path).sha256() == p.sha256()


def test_a_second_call_runs_nothing_and_only_runs_what_is_asked(tmp_path: Path) -> None:
    p, runs = plan(**SMALL), FakeRuns()
    run_benchmark(p, tmp_path, spec_path=SPEC_PATH, run_fn=runs, only=[2])
    assert [c["suffix"] for c in runs.calls] == ["_r2"]
    assert [r.status for r in load_manifest(tmp_path).runs] == ["pending", "done", "pending"]  # type: ignore[union-attr]
    run_benchmark(p, tmp_path, spec_path=SPEC_PATH, run_fn=runs)
    assert [c["suffix"] for c in runs.calls] == ["_r2", "_r1", "_r3"]
    run_benchmark(p, tmp_path, spec_path=SPEC_PATH, run_fn=runs)
    assert len(runs.calls) == 3
    with pytest.raises(RunnerError, match="--only"):
        run_benchmark(p, tmp_path, spec_path=SPEC_PATH, run_fn=runs, only=[9])


def test_an_interrupted_run_is_continued_from_its_journal(tmp_path: Path) -> None:
    p = plan(**SMALL)
    crashing = FakeRuns(crash_runs={"r2"})
    with pytest.raises(KeyboardInterrupt):
        run_benchmark(p, tmp_path, spec_path=SPEC_PATH, run_fn=crashing)
    manifest = load_manifest(tmp_path)
    assert manifest is not None
    assert [r.status for r in manifest.runs] == ["done", "failed", "pending"]
    assert manifest.runs[1].journal and "KeyboardInterrupt" in (manifest.runs[1].error or "")
    runs = FakeRuns()
    run_benchmark(p, tmp_path, spec_path=SPEC_PATH, run_fn=runs)
    resumed = runs.calls[0]
    assert resumed["resume"] == tmp_path / manifest.runs[1].journal and resumed["fresh"] is False
    assert runs.calls[1]["fresh"] is True
    assert [r.status for r in load_manifest(tmp_path).runs] == ["done"] * 3  # type: ignore[union-attr]


def test_redo_keeps_the_old_journal_as_abandoned_and_starts_again(tmp_path: Path) -> None:
    p = plan(**SMALL)
    run_benchmark(p, tmp_path, spec_path=SPEC_PATH, run_fn=FakeRuns())
    old = load_manifest(tmp_path).runs[0].journal  # type: ignore[union-attr]
    runs = FakeRuns()
    run_benchmark(p, tmp_path, spec_path=SPEC_PATH, run_fn=runs, redo=[1])
    assert [c["suffix"] for c in runs.calls] == ["_r1"] and runs.calls[0]["fresh"] is True
    entry = load_manifest(tmp_path).runs[0]  # type: ignore[union-attr]
    assert entry.abandoned == [f"{old}.abandoned"] and (tmp_path / f"{old}.abandoned").exists()
    assert entry.journal != old
    with pytest.raises(RunnerError, match="--redo"):
        run_benchmark(p, tmp_path, spec_path=SPEC_PATH, run_fn=runs, redo=[7])


def test_a_folder_belongs_to_one_plan(tmp_path: Path) -> None:
    run_benchmark(plan(**SMALL), tmp_path, spec_path=SPEC_PATH, run_fn=FakeRuns(), only=[1])
    with pytest.raises(RunnerError, match="different plan"):
        init_manifest(tmp_path, plan(arms=(Arm("S1-direct", 1),)))
    assert json.loads((tmp_path / MANIFEST).read_text(encoding="utf-8"))["plan_sha256"]


# -- statistics -----------------------------------------------------------------------------------


def test_stat_is_min_median_max_and_empty_is_n_zero() -> None:
    s = stat([5.0, 1.0, 3.0, 100.0])
    assert (s.n, s.min, s.median, s.max) == (4, 1.0, 4.0, 100.0)
    e = stat([])
    assert (e.n, e.min, e.median, e.max) == (0, None, None, None)


# -- aggregate and report, end to end over engine journals ---------------------------------------

LAGS = {
    "setup0": {"lag_s": 2.0},
    "setup_a_basic": {"lag_s": 120.0, "indexer_every_s": 90.0},
    "setup_b": {"lag_s": 6.0},
}


@pytest.fixture(scope="module")
def finished(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, BenchmarkPlan]:
    folder = tmp_path_factory.mktemp("bench")
    p = plan(**SMALL)
    run_benchmark(p, folder, spec_path=SPEC_PATH, run_fn=FakeRuns(LAGS))
    return folder, p


def summary_of(folder: Path, p: BenchmarkPlan):  # type: ignore[no-untyped-def]
    manifest = init_manifest(folder, p)
    return summarize(SPEC, p, manifest, load_runs(folder, p, manifest, SPEC))


def cell(s, scenario: str, system: str):  # type: ignore[no-untyped-def]
    return next(c for c in s.cells if c.scenario == scenario and c.system == system)


def test_the_summary_counts_replicates_and_computes_the_times(finished) -> None:  # type: ignore[no-untyped-def]
    folder, p = finished
    s = summary_of(folder, p)
    assert {(c.scenario, c.system): c.replicates for c in s.cells} == {
        (a, sys): n for a, n in (("S1-direct", 2), ("S1-entra", 3)) for sys in SYSTEMS
    }
    for system, lag in (("setup0", 2.0), ("setup_a_basic", 120.0), ("setup_b", 6.0)):
        c = cell(s, "S1-direct", system)
        assert c.first_deny.n == 2 and lag - 0.1 <= c.first_deny.min <= lag + 5.2
        assert c.did_not_converge == 0 and c.errors == 0
        assert c.baseline_status == {"BASELINE_OK": 2}
        assert all(r.complete for r in c.rows)
    assert (
        cell(s, "S1-direct", "setup0").verdicts and cell(s, "S1-entra", "setup_b").replicates == 3
    )


def test_by_baseline_rows_separate_the_alternating_runs(finished) -> None:  # type: ignore[no-untyped-def]
    folder, p = finished
    c = cell(summary_of(folder, p), "S1-entra", "setup0")
    assert [(b.baseline_minutes, b.replicates) for b in c.by_baseline] == [(1, 2), (10, 1)]
    assert [r.baseline_minutes for r in c.rows] == [1, 10, 1]


def test_indexer_runs_poller_lag_and_recall_are_recorded(finished) -> None:  # type: ignore[no-untyped-def]
    folder, p = finished
    s = summary_of(folder, p)
    assert {i.system for i in s.indexer_runs} == {"setup_a_basic"}
    assert all(i.runs > 0 and i.failed == 0 for i in s.indexer_runs)
    assert cell(s, "S1-direct", "setup_a_basic").first_indexer_run_end_after_change.n == 2
    lag = [x for x in s.poller_lag if x.system == "setup_b"]
    assert {x.run for x in lag} == {1, 2, 3} and all(x.samples > 0 for x in lag)
    assert lag[0].membership_age_s.median == 4.0 and lag[0].degraded_events == 0
    assert s.postfilter_recall["samples"] == 6  # two per run
    assert s.postfilter_recall["by_k"]["3"] == {
        "samples": 3, "returned": 6, "possible": 9, "short_of_k": 3, "lost": 3,
        "pages": 3, "max_pages": 1, "capped": 3,
    }  # fmt: skip


def test_the_caveats_are_generated_from_the_data(finished) -> None:  # type: ignore[no-untyped-def]
    folder, p = finished
    s = summary_of(folder, p)
    text = "\n".join(s.caveats)
    assert "between 2 and 3 complete replicates" in text
    assert "setup_a_basic" in text and "not a tuned or best-case" in text
    assert SETUP_B_LABEL in text
    assert "admin" in text and "out of scope" in text
    assert "Single tenant" in text
    assert "Dates: runs between" in text and s.generated_utc in text
    assert "1 min in runs [1, 3]; 10 min in runs [2]" in text
    assert f"Cells below {SMALL_N} replicates" in text and "S1-direct/setup0 (n=2)" in text
    personas = sum(1 for u in SPEC.users if u.persona)
    assert f"only the {personas} persona accounts" in text


def test_problems_in_the_data_are_counted_in_the_caveats_and_the_table(tmp_path: Path) -> None:
    p = plan(arms=(Arm("S1-direct", 1),), cap_s=300.0)
    never = [("p-plateng", c) for c in ("CANARY-S1D-01",)]  # replaced below by the real canaries
    from leaklab.spec import generate_canaries

    d1, d2 = generate_canaries(SPEC)["S1-Direct"]
    never = [("p-plateng", d1), ("p-plateng", d2)]
    runs = FakeRuns({"setup0": {"lag_s": 2.0}, "setup_b": {"never_revoke": never}})
    run_benchmark(p, tmp_path, spec_path=SPEC_PATH, run_fn=runs)
    s = summary_of(tmp_path, p)
    stuck = cell(s, "S1-direct", "setup_b")
    assert stuck.did_not_converge == 1 and stuck.never_denied == 1
    assert stuck.first_deny.n == 0 and stuck.first_deny.min is None
    assert cell(s, "S1-direct", "setup0").did_not_converge == 0
    text = "\n".join(s.caveats)
    assert "Data quality:" in text and "1 replicate(s) did not converge" in text
    assert "never refused a revoked pair" in text
    html = render_html(s)
    assert "DID_NOT_CONVERGE" in html and 'class="num bad">1<' in html


def test_an_incomplete_run_is_listed_but_not_counted(tmp_path: Path) -> None:
    p = plan(arms=(Arm("S1-direct", 2),), cap_s=300.0)
    runs = FakeRuns({"setup0": {"lag_s": 2.0}}, crash_runs={"r2"})
    with pytest.raises(KeyboardInterrupt):
        run_benchmark(p, tmp_path, spec_path=SPEC_PATH, run_fn=runs)
    s = summary_of(tmp_path, p)
    c = cell(s, "S1-direct", "setup0")
    assert c.replicates == 1 and c.planned == 2
    assert [r.status for r in s.runs] == ["done", "failed"]
    assert "Failed runs: run 2" in "\n".join(s.caveats)
    assert "3 planned replicate(s) have no result yet" in "\n".join(s.caveats)


# -- the report files -----------------------------------------------------------------------------


def test_report_files_hold_every_number_and_name_the_journals(finished) -> None:  # type: ignore[no-untyped-def]
    folder, p = finished
    s = summary_of(folder, p)
    json_path, html_path = write_report(s, folder)
    assert (json_path.name, html_path.name) == (JSON_NAME, HTML_NAME)
    data = json.loads(json_path.read_text(encoding="utf-8"))
    assert data["plan_sha256"] == p.sha256() and len(data["cells"]) == len(p.arms) * len(SYSTEMS)
    html = html_path.read_text(encoding="utf-8")
    for journal in data["journals"]:
        assert f'href="{journal}"' in html
    deny = cell(s, "S1-direct", "setup_a_basic").first_deny
    assert f"{deny.median / 60:.1f} min" in html  # the 120 s setup_a_basic lag shows as minutes
    assert SETUP_B_LABEL in html and "Caveats" in html and "Recall cost" in html


def test_html_escapes_text_from_the_data(finished) -> None:  # type: ignore[no-untyped-def]
    folder, p = finished
    s = summary_of(folder, p)
    s.caveats.append("<script>alert('x')</script>")
    html = render_html(s)
    assert "<script>alert" not in html and "&lt;script&gt;" in html


def test_the_template_contains_no_numbers_of_its_own() -> None:
    """Outside CSS, tags and template code the template has no digits (but the scenario name S6)."""
    text = (Path(__file__).parents[1] / "src/leaklab/benchmark/templates/report.html.j2").read_text(
        encoding="utf-8"
    )
    text = re.sub(r"<style>.*?</style>", "", text, flags=re.S)
    text = re.sub(r"\{%.*?%\}|\{\{.*?\}\}", "", text, flags=re.S)
    text = re.sub(r"<[^>]+>", "", text)
    assert re.findall(r"\d+", text.replace("&times;5", "").replace("S6", "")) == []


def test_report_command_builds_from_a_folder_without_a_tenant(finished) -> None:  # type: ignore[no-untyped-def]
    from typer.testing import CliRunner

    from leaklab.benchmark.cli import app

    folder, p = finished
    out = CliRunner().invoke(app, ["report", "--dir", str(folder), "--spec", str(SPEC_PATH)])
    assert out.exit_code == 0, out.output
    assert (folder / HTML_NAME).exists() and "3 journal(s) read" in out.output


def test_plan_command_writes_plan_json_and_refuses_a_foreign_folder(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from leaklab.benchmark.cli import app

    runner = CliRunner()
    first = runner.invoke(app, ["plan", "--dir", str(tmp_path / "b"), "--spec", str(SPEC_PATH)])
    assert first.exit_code == 0, first.output
    assert load_plan(tmp_path / "b").runs[0].scenarios[0] == "S1-direct"
    run_benchmark(
        build_plan(SPEC, "ignored", **SMALL),  # type: ignore[arg-type]
        tmp_path / "c",
        spec_path=SPEC_PATH,
        run_fn=FakeRuns(),
        only=[1],
    )
    clash = runner.invoke(app, ["plan", "--dir", str(tmp_path / "c"), "--spec", str(SPEC_PATH)])
    assert clash.exit_code == 2 and "different plan" in clash.output


# -- start-up, readiness, index cleanup and stale copies in the report -----------------------------


def test_startup_cleanup_and_readiness_reach_the_summary_caveats_and_html(finished) -> None:  # type: ignore[no-untyped-def]
    folder, p = finished
    s = summary_of(folder, p)
    assert [u.run for u in s.startup] == [1, 2, 3]
    first = s.startup[0]
    assert first.ready is True and first.time_to_ready_s == 41.5 and first.cap_s == 300.0
    assert first.complete_at_first_sync is False and first.difference == {"grants": 2}
    assert [c.run for c in s.index_cleanups] == [1, 2, 3]
    b, a = s.index_cleanups[0].indexes
    assert (b.index, b.count_before, b.count_after, b.stale_found, b.deleted) == (
        "ll-sp-index-b", 90, 45, 45, 45,
    )  # fmt: skip
    assert (a.count_before, a.count_after) == (90, 45)  # the filtered index's own statistic
    text = "\n".join(s.caveats)
    assert "Setup B start-up: the runner waited for /ready before the baseline in 3 run(s)" in text
    assert "time to ready 41.5-41.5 s" in text and "Lockouts are counted only from reads" in text
    assert "in 3 the state gained grants between the first sync and readiness" in text
    assert "Index cleanup: before 3 run(s)" in text and "270 deleted in total" in text
    html = render_html(s)
    assert "Setup B start-up" in html and "Index cleanup" in html and "ll-sp-index-b" in html


def test_a_run_that_was_not_ready_within_the_cap_is_flagged() -> None:
    from leaklab.benchmark.aggregate import BenchmarkSummary as BS
    from leaklab.benchmark.aggregate import StartupSummary, _startup_caveats

    summary = BS.model_construct(
        startup=[
            StartupSummary(
                run=2, ready=False, time_to_ready_s=None, waited_in_prepare_s=300.0, cap_s=300.0,
                first_sync={}, difference={}, complete_at_first_sync=None, found_by_verify=None,
            )
        ],
        index_cleanups=[],
        stale_copies=[],
    )  # fmt: skip
    [text] = _startup_caveats(summary)
    assert "run(s) [2] were not ready within the cap" in text and "time to ready unknown" in text


def test_stale_copies_are_counted_per_run_and_system() -> None:
    from leaklab.benchmark.aggregate import LoadedRun, _stale
    from leaklab.runner import build_report

    header = {
        "type": "run_started", "run_id": "r", "scenarios": [], "systems": ["setup_a_basic"],
        "config": {"n_consecutive": 3, "cap_s": 60.0},
    }  # fmt: skip
    events = [
        header,
        {"type": "system_event", "system": "setup_a_basic", "kind": "stale_copy_seen",
         "canary": "C1"},
        {"type": "system_event", "system": "setup_a_basic", "kind": "stale_copy_seen",
         "canary": "C2"},
        {"type": "system_event", "system": "setup_a_basic", "kind": "stale_copy_seen",
         "canary": "C1"},
    ]  # fmt: skip
    run = LoadedRun(1, "r1", "x.jsonl", 1, events, build_report(SPEC, events))
    [row] = _stale([run])
    assert (row.run, row.system, row.stale_canaries) == (1, "setup_a_basic", 2)


# -- --arm / --quick, and extending a finished benchmark set --------------------------------


def test_arms_are_parsed_from_scenario_equals_n() -> None:
    from leaklab.benchmark.matrix import parse_arms, quick_arms

    arms = parse_arms(["S1-direct=2", "S1-entra=4", "S4-move-into-restricted=1"])
    assert [(a.scenario, a.replicates, a.baseline_alternates) for a in arms] == [
        ("S1-direct", 2, False),
        ("S1-entra", 4, True),  # a default arm keeps its alternating baseline
        ("S4-move-into-restricted", 1, False),
    ]
    for bad in (["S1-direct"], ["S1-direct=0"], ["S1-direct=x"], ["S1-direct=1", "S1-direct=2"]):
        with pytest.raises(RunnerError):
            parse_arms(bad)
    assert [(a.scenario, a.replicates) for a in quick_arms()] == [
        (a.scenario, 1) for a in DEFAULT_ARMS
    ]


def test_a_quick_plan_is_one_run_with_every_scenario_in_one_loop() -> None:
    from leaklab.benchmark.matrix import quick_arms

    p = plan(arms=quick_arms())
    assert len(p.runs) == 1 and p.runs[0].baseline_minutes == 1
    assert len(p.runs[0].loops) == 1 and len(p.runs[0].scenarios) == len(DEFAULT_ARMS)


def test_extending_adds_runs_that_continue_the_baseline_alternation() -> None:
    from leaklab.benchmark.matrix import extend_plan, quick_arms

    p = plan(arms=quick_arms())
    longer = extend_plan(SPEC, p, ["S1-entra", "S2-nested-group-removal"], 3)
    assert [r.index for r in longer.runs] == [1, 2, 3, 4]
    assert longer.runs[0] == p.runs[0]  # the first run is untouched
    assert [r.baseline_minutes for r in longer.runs] == [1, 10, 1, 10]
    assert all(r.scenarios == ["S1-entra", "S2-nested-group-removal"] for r in longer.runs[1:])
    assert all(r.config["cap_s"] == 3600.0 and r.config["interval_s"] == 5.0 for r in longer.runs)
    assert {a.scenario: a.replicates for a in longer.arms}["S1-entra"] == 4
    assert {a.scenario: a.replicates for a in longer.arms}["S1-direct"] == 1
    assert longer.sha256() != p.sha256()
    with pytest.raises(RunnerError, match="unknown"):
        extend_plan(SPEC, p, ["nope"], 1)
    with pytest.raises(RunnerError):
        extend_plan(SPEC, p, ["S1-entra"], 0)


def test_extending_a_finished_set_appends_runs_and_their_journals_to_the_same_folder(
    tmp_path: Path,
) -> None:
    from leaklab.benchmark.execute import extend_folder
    from leaklab.benchmark.matrix import extend_plan

    p = plan(arms=(Arm("S1-direct", 1), Arm("S1-entra", 1, baseline_alternates=True)), cap_s=900.0)
    run_benchmark(p, tmp_path, spec_path=SPEC_PATH, run_fn=FakeRuns(LAGS))
    first_journal = load_manifest(tmp_path).runs[0].journal  # type: ignore[union-attr]
    longer = extend_plan(SPEC, p, ["S1-entra"], 2)
    manifest = extend_folder(tmp_path, longer)
    assert [(r.index, r.status) for r in manifest.runs] == [
        (1, "done"),
        (2, "pending"),
        (3, "pending"),
    ]
    assert (
        manifest.plan_sha256 == longer.sha256() and load_plan(tmp_path).sha256() == longer.sha256()
    )
    runs = FakeRuns(LAGS)
    run_benchmark(load_plan(tmp_path), tmp_path, spec_path=SPEC_PATH, run_fn=runs)
    assert [c["suffix"] for c in runs.calls] == ["_r2", "_r3"]  # run 1 was done and is not repeated
    assert [c["scenarios"] for c in runs.calls] == [["S1-entra"], ["S1-entra"]]
    assert [c["config"].baseline_k for c in runs.calls] == [120, 12]
    s = summary_of(tmp_path, load_plan(tmp_path))
    entra = cell(s, "S1-entra", "setup0")
    assert entra.replicates == 3 and [r.run for r in entra.rows] == [1, 2, 3]
    assert load_manifest(tmp_path).runs[0].journal == first_journal  # type: ignore[union-attr]
    assert len(s.journals) == 3


def test_an_extension_that_rewrites_earlier_runs_is_refused(tmp_path: Path) -> None:
    from leaklab.benchmark.execute import extend_folder
    from leaklab.benchmark.matrix import extend_plan

    p = plan(arms=(Arm("S1-direct", 1),))
    run_benchmark(p, tmp_path, spec_path=SPEC_PATH, run_fn=FakeRuns(), only=[1])
    other = extend_plan(SPEC, plan(arms=(Arm("S1-direct", 1),), cap_s=1800.0), ["S1-direct"], 1)
    with pytest.raises(RunnerError, match="changes runs that already exist"):
        extend_folder(tmp_path, other)


def test_the_extend_and_quick_commands_and_run_uses_the_stored_plan(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from leaklab.benchmark.cli import app

    runner = CliRunner()
    folder = str(tmp_path / "set")
    out = runner.invoke(app, ["plan", "--dir", folder, "--spec", str(SPEC_PATH), "--quick"])
    assert out.exit_code == 0, out.output
    assert len(load_plan(Path(folder)).runs) == 1
    out = runner.invoke(app, ["extend", "--dir", folder, "--spec", str(SPEC_PATH), "--runs", "2"])
    assert out.exit_code == 0, out.output
    stored = load_plan(Path(folder))
    assert [r.index for r in stored.runs] == [1, 2, 3]
    assert stored.runs[2].scenarios == ["S1-entra", "S2-nested-group-removal"]
    assert "pending runs: [1, 2, 3]" in out.output
    both = runner.invoke(
        app, ["plan", "--dir", str(tmp_path / "x"), "--arm", "S1-direct=1", "--quick"]
    )
    assert both.exit_code == 2 and "either --arm or --quick" in both.output


def test_a_listable_index_is_counted_by_search_even_when_the_count_is_zero() -> None:
    from leaklab.benchmark.aggregate import _cleanup_index

    row = _cleanup_index(
        {
            "index": "ll-sp-index-b",
            "listed": 45,
            "count_search_before": 45,
            "count_search_after": 0,  # all stale documents gone, the new generation not indexed yet
            "count_stats_before": 45,
            "count_stats_after": 45,  # the statistic lags and must not be shown instead
            "stale_found": 45,
            "deleted": 45,
        }
    )
    assert (row.count_before, row.count_after, row.deleted) == (45, 0, 45)
    filtered = _cleanup_index(
        {"index": "ll-sp-index", "listed": None, "count_search_before": 0, "count_stats_before": 45,
         "count_search_after": 0, "count_stats_after": 20, "stale_found": 25, "deleted": 25}
    )  # fmt: skip
    assert (filtered.count_before, filtered.count_after) == (45, 20)
