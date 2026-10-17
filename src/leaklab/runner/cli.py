"""Command line of the scenario runner.

  run     apply scenarios to the generated tenant and read them through the chosen systems
  report  rebuild the results table from a run journal (no network)

Examples:
  uv run python -m leaklab.runner run --scenario S1-direct --scenario S1-spgroup --systems setup0
  uv run python -m leaklab.runner run --scenario "S1-*" --systems setup0 --fresh --replicate 2
  uv run python -m leaklab.runner run --scenario S1-entra --systems setup0 --resume runs/run_X.jsonl
  uv run python -m leaklab.runner report --run runs/run_X.jsonl

A run MUTATES the tenant. Use --fresh (teardown + generate first) for each independent run.
The journals and reports go to runs/ (git-ignored) with a UTC timestamp in the name.
"""

import fnmatch
import json
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from leaklab.runner.journal import load_events
from leaklab.runner.metrics import RunReport, build_report
from leaklab.runner.model import RunConfig, RunnerError
from leaklab.runner.run import run_once
from leaklab.spec import load_spec

ROOT = Path(__file__).resolve().parents[3]
console = Console()
app = typer.Typer(add_completion=False, no_args_is_help=True)

SPEC_OPT = typer.Option(ROOT / "tenant_spec.yaml", help="Path to tenant_spec.yaml.")
OUT_OPT = typer.Option(ROOT / "runs", help="Where journals and reports are written.")


def expand_scenarios(patterns: list[str], known: list[str]) -> list[str]:
    """Scenario ids from names or shell-style patterns ("S1-*"), in the spec's order, no repeats."""
    chosen: list[str] = []
    for pattern in patterns:
        for part in pattern.split(","):
            matches = [k for k in known if fnmatch.fnmatchcase(k, part.strip())]
            if not matches:
                raise RunnerError(f"no scenario matches {part!r}; the spec has {known}")
            chosen += [m for m in matches if m not in chosen]
    return chosen


def _fmt(value: float | None) -> str:
    return "-" if value is None else f"{value:.1f}"


def render(report: RunReport) -> None:
    out = console if console.is_terminal else Console(width=170)  # piped output: do not cut columns
    table = Table(title=f"run {report.run_id}")
    for column in (
        "scenario",
        "system",
        "baseline",
        "verdict",
        "1st deny s",
        "leak s",
        "conformance s",
        "lockout s",
        "idx run s",
        "err",
        "reads",
    ):
        table.add_column(column, no_wrap=column in ("scenario", "system", "baseline", "verdict"))
    for r in report.results:
        conformance = (
            _fmt(r.time_to_conformance_s) if r.convergence == "CONFORMED" else r.convergence
        )
        table.add_row(
            r.scenario,
            r.system,
            r.baseline_status,
            r.verdict,
            _fmt(r.time_to_first_deny_s),
            _fmt(r.leak_exposure_s),
            conformance,
            _fmt(r.lockout_s),
            _fmt(r.first_indexer_run_end_after_change_s),
            str(r.errors),
            str(r.observations),
        )
    out.print(table)
    for note in report.notes:
        out.print(f"[yellow]note: {note}[/yellow]")


@app.command("run")
def cmd_run(
    scenario: list[str] = typer.Option(..., "--scenario", "-s", help="Scenario id or pattern."),
    systems: str = typer.Option("setup0", help="Comma-separated systems (setup0)."),
    replicate: int = typer.Option(1, min=1, help="Repeat n times, each on a fresh tenant."),
    fresh: bool = typer.Option(False, help="Tear down and generate before the run."),
    teardown_after: bool = typer.Option(False, help="Tear the tenant down when the run ends."),
    resume: Path | None = typer.Option(None, help="Continue the run in this journal."),
    baseline_k: int = typer.Option(5, help="Conforming cycles in a row before the mutation."),
    baseline_cap_s: float = typer.Option(900.0, help="Give up on a stable baseline after this."),
    n: int = typer.Option(10, help="Conforming reads in a row that count as converged."),
    cap_s: float = typer.Option(3600.0, help="Stop polling this long after the mutation."),
    interval_s: float = typer.Option(10.0, help="Seconds between read cycles."),
    steady_window_s: float = typer.Option(
        120.0, help="Watch time for scenarios that flip nothing."
    ),
    workers: int = typer.Option(4, min=1, help="Parallel reads inside a cycle."),
    sku_part_number: str = typer.Option("", help="License SKU part number, if ambiguous."),
    spec_path: Path = SPEC_OPT,
    out_dir: Path = OUT_OPT,
) -> None:
    """Run scenarios against the real test tenant."""
    try:
        known = [s.id for s in load_spec(spec_path).scenarios]
        ids = expand_scenarios(scenario, known)
        config = RunConfig(
            baseline_k=baseline_k,
            baseline_cap_s=baseline_cap_s,
            n_consecutive=n,
            cap_s=cap_s,
            interval_s=interval_s,
            steady_window_s=steady_window_s,
            workers=workers,
        )
        names = [s.strip() for s in systems.split(",") if s.strip()]
        if resume is not None and replicate > 1:
            raise RunnerError("--resume continues one run; it cannot be combined with --replicate")
        for index in range(replicate):
            suffix = f"_r{index + 1}" if replicate > 1 else ""
            console.print(f"[bold]run {index + 1} of {replicate}: {ids} on {names}[/bold]")
            path, report = run_once(
                spec_path=spec_path,
                scenario_ids=ids,
                system_names=names,
                config=config,
                out_dir=out_dir,
                fresh=fresh or index > 0,
                resume=resume,
                sku_part_number=sku_part_number,
                teardown_after=teardown_after,
                suffix=suffix,
            )
            render(report)
            console.print(f"journal: {path}")
    except RunnerError as exc:
        console.print(f"[bold red]{exc}[/bold red]")
        raise typer.Exit(code=2) from exc


@app.command("report")
def cmd_report(
    run: Path = typer.Option(..., "--run", help="A run journal (runs/run_*.jsonl)."),
    as_json: bool = typer.Option(False, "--json", help="Print JSON instead of a table."),
    spec_path: Path = SPEC_OPT,
) -> None:
    """Rebuild the results of a run from its journal."""
    report = build_report(load_spec(spec_path), load_events(run))
    if as_json:
        console.print_json(json.dumps(report.model_dump()))
    else:
        render(report)
