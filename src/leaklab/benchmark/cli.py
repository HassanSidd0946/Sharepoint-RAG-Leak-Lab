"""`python -m leaklab.benchmark plan | run | report`.

  plan    work out the matrix (no network): runs, loops, baselines, the longest it can take; writes
          plan.json into the benchmark folder
  run     execute the planned runs on the REAL test tenant, resumably (hours): every run tears the
          tenant down and generates it again; use --only / --redo to pick runs
  report  rebuild report.json and report.html from the journals in the folder (no network)

Examples:
  uv run python -m leaklab.benchmark plan
  uv run python -m leaklab.benchmark run
  uv run python -m leaklab.benchmark run --only 1 --only 2
  uv run python -m leaklab.benchmark report
"""

from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from leaklab.benchmark.aggregate import load_runs, summarize
from leaklab.benchmark.execute import (
    PLAN,
    init_manifest,
    load_manifest,
    load_plan,
    run_benchmark,
    write_plan,
)
from leaklab.benchmark.matrix import SYSTEMS, BenchmarkPlan, build_plan
from leaklab.benchmark.render import write_report
from leaklab.runner import RunnerError
from leaklab.runner.run import run_once, spec_sha256
from leaklab.spec import load_spec

console = Console()
app = typer.Typer(add_completion=False, no_args_is_help=True)
DIR_OPT = typer.Option(Path("runs/benchmark"), "--dir", help="The benchmark folder.")
SPEC_OPT = typer.Option(Path("tenant_spec.yaml"), "--spec", help="tenant_spec.yaml")


def _plan_from_options(
    spec_path: Path, systems: str, interval_s: float, n: int, cap_s: float, workers: int
) -> BenchmarkPlan:
    names = tuple(s.strip() for s in systems.split(",") if s.strip())
    return build_plan(
        load_spec(spec_path),
        spec_sha256(spec_path),
        systems=names,
        interval_s=interval_s,
        n_consecutive=n,
        cap_s=cap_s,
        workers=workers,
    )


def _show_plan(plan: BenchmarkPlan) -> None:
    table = Table(title="Benchmark plan", show_lines=False)
    for col in ("run", "baseline", "polling loops", "scenarios", "polling at most"):
        table.add_column(col)
    for r in plan.runs:
        table.add_row(
            str(r.index),
            f"{r.baseline_minutes} min (k={int(r.config['baseline_k'])})",
            str(len(r.loops)),
            ", ".join(r.scenarios),
            f"{r.max_polling_s / 3600:.1f} h",
        )
    console.print(table)
    console.print(f"systems: {', '.join(plan.systems)}")
    console.print("replicates: " + ", ".join(f"{a.scenario} x{a.replicates}" for a in plan.arms))
    console.print(
        f"polling alone takes at most {plan.max_total_polling_s / 3600:.1f} h; add teardown, "
        "generation and settling (several minutes) to every run"
    )
    for note in plan.notes:
        console.print(f"[yellow]note: {note}[/yellow]")


@app.command("plan")
def cmd_plan(
    folder: Path = DIR_OPT,
    spec_path: Path = SPEC_OPT,
    systems: str = typer.Option(",".join(SYSTEMS), help="Comma-separated systems."),
    interval_s: float = typer.Option(5.0, help="Seconds between read cycles."),
    n: int = typer.Option(10, help="Conforming reads in a row that count as converged."),
    cap_s: float = typer.Option(3600.0, help="Stop polling this long after the change."),
    workers: int = typer.Option(4, min=1, help="Parallel reads inside a cycle."),
) -> None:
    """Work out the matrix and write plan.json (nothing is run)."""
    try:
        plan = _plan_from_options(spec_path, systems, interval_s, n, cap_s, workers)
        existing = load_manifest(folder)
        if existing is not None and existing.plan_sha256 != plan.sha256():
            raise RunnerError(f"{folder} already has runs for a different plan; use another --dir")
        write_plan(folder, plan)
    except RunnerError as exc:
        console.print(f"[bold red]{exc}[/bold red]")
        raise typer.Exit(code=2) from exc
    _show_plan(plan)
    console.print(f"written: {folder / PLAN}")


@app.command("run")
def cmd_run(
    folder: Path = DIR_OPT,
    spec_path: Path = SPEC_OPT,
    systems: str = typer.Option(",".join(SYSTEMS), help="Comma-separated systems."),
    interval_s: float = typer.Option(5.0, help="Seconds between read cycles."),
    n: int = typer.Option(10, help="Conforming reads in a row that count as converged."),
    cap_s: float = typer.Option(3600.0, help="Stop polling this long after the change."),
    workers: int = typer.Option(4, min=1, help="Parallel reads inside a cycle."),
    only: list[int] = typer.Option([], "--only", help="Run only this run number (repeatable)."),
    redo: list[int] = typer.Option([], "--redo", help="Abandon this run's journal and redo it."),
    sku_part_number: str = typer.Option("", help="License SKU part number, if ambiguous."),
) -> None:
    """Execute the runs that are not done yet on the real tenant (resumes where it stopped)."""
    try:
        plan = _plan_from_options(spec_path, systems, interval_s, n, cap_s, workers)
        _show_plan(plan)
        manifest = run_benchmark(
            plan,
            folder,
            spec_path=spec_path,
            run_fn=run_once,
            only=only or None,
            redo=redo,
            sku_part_number=sku_part_number,
            log=console.print,
        )
    except RunnerError as exc:
        console.print(f"[bold red]{exc}[/bold red]")
        raise typer.Exit(code=2) from exc
    done = sum(1 for r in manifest.runs if r.status == "done")
    console.print(f"{done} of {len(manifest.runs)} runs done. Next: `benchmark report`.")


@app.command("report")
def cmd_report(folder: Path = DIR_OPT, spec_path: Path = SPEC_OPT) -> None:
    """Rebuild report.json and report.html from the journals (no network)."""
    try:
        plan = load_plan(folder)
        spec = load_spec(spec_path)
        if plan.spec_sha256 != spec_sha256(spec_path):
            console.print(
                "[yellow]note: tenant_spec.yaml changed since the plan was made; the report "
                "is built with the CURRENT spec[/yellow]"
            )
        manifest = init_manifest(folder, plan)
        loaded = load_runs(folder, plan, manifest, spec)
        summary = summarize(spec, plan, manifest, loaded)
        json_path, html_path = write_report(summary, folder)
    except RunnerError as exc:
        console.print(f"[bold red]{exc}[/bold red]")
        raise typer.Exit(code=2) from exc
    done = sum(1 for r in manifest.runs if r.status == "done")
    console.print(f"{len(loaded)} journal(s) read, {done} of {len(manifest.runs)} runs done")
    console.print(f"written: {json_path}\nwritten: {html_path}")
