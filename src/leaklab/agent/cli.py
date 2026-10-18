"""`python -m leaklab.agent ...`

  s6 run      run Scenario S6 on the live test tenant (fresh generation, cleaned indexes, all three
              agent arms, n trials per variant) into <dir>/s6_<UTC>.jsonl. StubLLM only.
  s6 report   rebuild the S6 table from the newest journal in <dir> and print it (the full benchmark
              report, `python -m leaklab.benchmark report`, includes it too)
  explore     EXPLORATORY: ask the S6a questions once through a REAL model (`--real-llm`). Its
              journal is s6x_*; it is never read by a report and never gives benchmark numbers.

`s6 run --real-llm` is refused on purpose.
"""

from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from leaklab.agent.llm import RealLLM
from leaklab.agent.s6.live import run_s6_live
from leaklab.agent.s6.report import load_s6_report
from leaklab.runner import RunnerError
from leaklab.spec import load_spec

console = Console()
app = typer.Typer(add_completion=False, no_args_is_help=True)
s6 = typer.Typer(add_completion=False, no_args_is_help=True)
app.add_typer(s6, name="s6", help="Scenario S6: agent cache and memory.")
DIR_OPT = typer.Option(Path("runs/benchmark_v2"), "--dir", help="The benchmark folder.")
SPEC_OPT = typer.Option(Path("tenant_spec.yaml"), "--spec", help="tenant_spec.yaml")


@s6.command("run")
def s6_run(
    folder: Path = DIR_OPT,
    spec_path: Path = SPEC_OPT,
    n: int = typer.Option(3, min=1, help="Trials per variant per arm (the query set has 3)."),
    settle_s: float = typer.Option(30.0, help="Wait after a confirmed revocation (S6b)."),
    fresh: bool = typer.Option(True, help="Tear down and generate the tenant first."),
    agent_db: str = typer.Option("embedded", help='"embedded" (pgserver) or a pgvector DSN.'),
    real_llm: bool = typer.Option(False, "--real-llm", help="REFUSED: not for benchmark numbers."),
) -> None:
    """Run S6 on the live tenant."""
    if real_llm:
        console.print(
            "[bold red]--real-llm is refused: S6 benchmark numbers use the stub LLM only.[/bold red]"
        )
        raise typer.Exit(code=2)
    try:
        path = run_s6_live(
            spec_path=spec_path,
            out_dir=folder,
            n=n,
            settle_s=settle_s,
            fresh=fresh,
            agent_dsn=None if agent_db == "embedded" else agent_db,
            log=console.print,
        )
    except RunnerError as exc:
        console.print(f"[bold red]{exc}[/bold red]")
        raise typer.Exit(code=2) from exc
    console.print(f"journal: {path}. Next: `s6 report` or `benchmark report`.")


@s6.command("report")
def s6_report(folder: Path = DIR_OPT, spec_path: Path = SPEC_OPT) -> None:
    """Print the S6 table of the newest journal in the folder."""
    report = load_s6_report(folder, load_spec(spec_path))
    if report is None:
        console.print(f"[red]no s6_*.jsonl journal in {folder}[/red]")
        raise typer.Exit(code=2)
    table = Table(
        title=f"S6 ({report.journal}, generation {report.generation}, query set v{report.query_version})"
    )
    for col in (
        "variant",
        "arm",
        "trials",
        "verdicts",
        "valid",
        "guard denials",
        "exposure unsafe",
    ):
        table.add_column(col)
    for c in report.cells:
        table.add_row(
            c.variant,
            c.arm,
            str(c.trials),
            ", ".join(f"{k} {v}" for k, v in sorted(c.verdicts.items())),
            str(c.valid_trials),
            str(c.guard_denials),
            str(c.exposure_unsafe),
        )
    console.print(table)
    for text in report.caveats:
        console.print(f"[yellow]- {text}[/yellow]")


@app.command("explore")
def explore(
    folder: Path = DIR_OPT,
    spec_path: Path = SPEC_OPT,
    real_llm: bool = typer.Option(
        False, "--real-llm", help="Use a real model (needs ANTHROPIC_API_KEY)."
    ),
) -> None:
    """EXPLORATORY run of S6a with a real model on the current tenant. Never for benchmark numbers."""
    if not real_llm:
        console.print("explore needs --real-llm (the stub is what `s6 run` uses).")
        raise typer.Exit(code=2)
    try:
        llm = RealLLM()
        path = run_s6_live(
            spec_path=spec_path,
            out_dir=folder,
            n=1,
            fresh=False,
            exploratory=True,
            llm=llm,
            variants=("S6a",),
            log=console.print,
        )
    except (RunnerError, RuntimeError) as exc:
        console.print(f"[bold red]{exc}[/bold red]")
        raise typer.Exit(code=2) from exc
    console.print(f"EXPLORATORY journal (not a benchmark result): {path}")
