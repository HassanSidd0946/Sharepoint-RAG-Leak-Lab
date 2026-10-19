"""`python -m leaklab.replay export | figures`.

export   journals + reports of a benchmark folder -> replay bundles (schema v1.0) and index.json.
         Exit code 3 if a bundle had to be SKIPPED (for example a status the schema cannot express):
         the others are written and the reasons are printed, nothing is renamed or made up.
figures  the two write-up figures (SVG + PNG) from exported bundles; the timeline is drawn for the
         bundle that best shows S1-entra unless --bundle names another.

uv run python -m leaklab.replay export --runs runs/benchmark_v2 --out runs/replays
uv run python -m leaklab.replay figures --bundles runs/replays --out runs/figures
"""

from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from leaklab.replay.build_public import BuildError, build_public
from leaklab.replay.export import ExportError, export_folder, write_bundles
from leaklab.replay.figures import (
    best_demo_bundle,
    bundle_name,
    first_deny_figure,
    load_bundles,
    representative_name,
    timeline_figure,
)
from leaklab.replay.publish_check import check as publish_check
from leaklab.replay.redact import RedactionError
from leaklab.replay.summary import SummaryMismatch
from leaklab.replay.summary_export import write_summary
from leaklab.replay.writeup import WriteupError, build_context, fill, format_table
from leaklab.runner.run import spec_sha256
from leaklab.spec import load_spec

console = Console()
app = typer.Typer(add_completion=False, no_args_is_help=True)
SPEC_OPT = typer.Option(Path("tenant_spec.yaml"), "--spec", help="tenant_spec.yaml")


@app.command("export")
def cmd_export(
    runs: Path = typer.Option(
        ..., "--runs", help="A benchmark folder (run_*.jsonl and run_*.report.json)."
    ),
    out: Path = typer.Option(..., "--out", help="Where the bundles and index.json are written."),
    spec_path: Path = SPEC_OPT,
) -> None:
    """Export every run journal of the folder to replay bundles."""
    try:
        result = export_folder(runs, load_spec(spec_path), spec_sha256(spec_path))
    except (ExportError, SummaryMismatch, RedactionError) as exc:
        console.print(f"[bold red]export failed: {exc}[/bold red]")
        raise typer.Exit(code=2) from exc
    paths = write_bundles(result, out)
    spec = load_spec(spec_path)
    extra = write_summary(
        runs, out, spec, [b.data for b in result.bundles if b.data["kind"] == "agent_trial"]
    )
    for f in extra:
        console.print(f"wrote {f}")
    table = Table(title=f"{len(paths)} bundle(s) written to {out}")
    for col in ("bundle", "systems", "summary (first deny s / status)"):
        table.add_column(col)
    for b in result.bundles:
        if b.data["kind"] == "agent_trial":
            parts = f"{b.data['summary']['verdict']} ({b.data['validity']})"
            table.add_row(b.name, "agent", parts)
            continue
        parts = ", ".join(
            f"{s['system']}: {'-' if s['first_deny_s'] is None else format(s['first_deny_s'], '.1f')} / {s['status']}"
            for s in b.data["summary"]
        )
        table.add_row(b.name, str(len(b.data["run"]["systems"])), parts)
    console.print(table)
    for skipped in result.skipped:
        console.print(
            f"[bold red]SKIPPED {skipped.run_id} / {skipped.scenario}: {skipped.reason}[/bold red]"
        )
    if result.skipped:
        raise typer.Exit(code=3)


@app.command("figures")
def cmd_figures(
    bundles: Path = typer.Option(..., "--bundles", help="A folder written by `export`."),
    out: Path = typer.Option(Path("runs/figures"), "--out"),
    bundle: str = typer.Option(
        "", "--bundle", help="Bundle name for the timeline (default: the best S1-entra one)."
    ),
    axis: str = typer.Option("symlog", help="Time axis of the timeline: symlog or linear."),
) -> None:
    """Draw the timeline of one bundle and the first-deny dot plot of all of them."""
    if axis not in ("symlog", "linear"):
        raise typer.BadParameter("axis must be symlog or linear")
    loaded = load_bundles(bundles)
    if not loaded:
        console.print("[bold red]no bundles in that folder[/bold red]")
        raise typer.Exit(code=2)
    chosen = (
        next((b for b in loaded if bundle_name(b) == bundle), None)
        if bundle
        else next(
            (b for b in loaded if bundle_name(b) == representative_name(bundles, "S1-entra")), None
        )
        or best_demo_bundle(loaded)
    )
    if chosen is None:
        console.print(
            "[bold red]no suitable bundle for the timeline (name one with --bundle)[/bold red]"
        )
        raise typer.Exit(code=2)
    files = timeline_figure(chosen, loaded, out / f"timeline__{bundle_name(chosen)}__{axis}", axis)
    files += first_deny_figure(loaded, out / "first_deny_dotplot")
    console.print(f"timeline of {bundle_name(chosen)}")
    for f in files:
        console.print(f"  {f}")


@app.command("writeup")
def cmd_writeup(
    template: Path = typer.Option(Path("docs/writeup/WRITEUP.template.md"), "--template"),
    report: Path = typer.Option(Path("runs/benchmark_v2/report.json"), "--report"),
    replays: Path = typer.Option(..., "--replays", help="A folder written by `export`."),
    meta: Path = typer.Option(Path("docs/writeup/meta.yaml"), "--meta"),
    out: Path = typer.Option(Path("docs/writeup/WRITEUP.md"), "--out"),
    video: Path = typer.Option(Path("docs/video/VIDEO_SCRIPT.md"), "--video"),
    video_out: Path = typer.Option(Path("docs/video/VIDEO_SCRIPT.filled.md"), "--video-out"),
    figures: Path = typer.Option(Path("runs/figures"), "--figures"),
    spec_path: Path = SPEC_OPT,
    site_meta: Path = typer.Option(Path("optimus-main/public/meta.json"), "--site-meta"),
    env_example: Path = typer.Option(Path(".env.example"), "--env-example"),
    allow_unset_meta: bool = typer.Option(
        False,
        "--allow-unset-meta",
        help="Dry run: meta values still TODO are filled as [unset: key].",
    ),
) -> None:
    """Fill the write-up (and the video script) from the report and the bundles; fail on any bad token."""
    ctx = build_context(
        report,
        replays,
        meta,
        load_spec(spec_path),
        out.parent,
        figures,
        allow_unset_meta,
        site_meta=site_meta,
        env_example=env_example,
    )
    jobs = [(template, out)]
    if video.exists():
        jobs.append((video, video_out))
    else:
        console.print(f"[yellow]{video} does not exist: the video script is not filled[/yellow]")
    try:
        for source, target in jobs:
            ctx.out_dir = target.parent
            filled = fill(source.read_text(encoding="utf-8"), ctx)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(filled, encoding="utf-8")
            console.print(f"wrote {target}")
    except WriteupError as exc:
        console.print(f"[bold red]cannot fill {source}:[/bold red]\n{exc}")
        raise typer.Exit(code=2) from exc
    table = Table(title="every token, its value and where it came from")
    for col in ("token", "value", "source"):
        table.add_column(col, overflow="fold")
    for row in format_table(ctx.rows):
        table.add_row(*row)
    console.print(table)


@app.command("publish-check")
def cmd_publish_check(root: Path = typer.Option(Path("."), "--root")) -> None:
    """Check docs/PUBLISH.md against git: private or unlisted files tracked, and identifiers in public files."""
    wrong, findings = publish_check(root.resolve())
    for kind, names in wrong.items():
        by_top: dict[str, int] = {}
        for n in names:
            by_top[n.split("/")[0]] = by_top.get(n.split("/")[0], 0) + 1
        console.print(
            f"{kind} files that git would publish: {len(names)} {by_top if names else ''}"
        )
    for f in findings:
        console.print(f"{f.path}:{f.line} {f.kind}")
    console.print(f"findings in public files: {len(findings)}")
    if findings or any(wrong.values()):
        raise typer.Exit(code=1)


@app.command("build-public")
def cmd_build_public(
    out: Path = typer.Option(..., "--out", help="A NEW (empty) folder for the public repository."),
    root: Path = typer.Option(Path("."), "--root"),
    skip_gitleaks: bool = typer.Option(
        False, "--skip-gitleaks", help="Do not run gitleaks (not for a release)."
    ),
) -> None:
    """Build the public repository from the allowlist: one clean commit, tenant_spec.yaml with placeholders."""
    from leaklab.replay.build_public import run_gitleaks

    try:
        report = build_public(root.resolve(), out, gitleaks=None if skip_gitleaks else run_gitleaks)
    except BuildError as exc:
        console.print(f"[bold red]build-public stopped:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
    for step in report.steps:
        console.print(step)
    console.print(
        f"built {out} ({len(report.files)} files, commit {report.commit}). Nothing was pushed."
    )
