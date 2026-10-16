"""Command line and environment opener for the generator.

open_env() is the ONLY place that connects to a tenant: it first calls assert_test_tenant() on the
tenant ID of both app tokens (Graph and SharePoint) and aborts if either is not the allowed test
tenant, then builds the gateways. Commands:

  plan      what would be created (no network)
  generate  create / complete the tenant (idempotent)
  verify    compare the tenant with the spec through REST/Graph
  teardown  remove everything this spec generated (prefix-named objects only)

Reports are written to runs/ with a UTC timestamp in the name (never overwritten).

Run:  uv run python -m leaklab.generator <command> [--spec-path tenant_spec.yaml]
"""

import json
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import typer
from rich.console import Console
from rich.pretty import Pretty

from leaklab.auth import GRAPH_DEFAULT_SCOPE, get_app_token, sp_scope
from leaklab.config import assert_test_tenant, get_settings, redact_jwt
from leaklab.generator.env import Env, GenSettings
from leaklab.generator.gateways import GraphGateway, SharePointGateway
from leaklab.generator.generate import generate
from leaklab.generator.log import MutationLog
from leaklab.generator.plan import GeneratorError, build_plan
from leaklab.generator.teardown import teardown
from leaklab.generator.verify import verify
from leaklab.graph_client import GraphClient
from leaklab.sp_rest import SharePointRest
from leaklab.spec import load_spec

ROOT = Path(__file__).resolve().parents[3]
console = Console()
app = typer.Typer(add_completion=False, no_args_is_help=True)


def guard_test_tenant() -> None:
    """Abort unless both tokens belong to the allowed test tenant (CLAUDE.md rule 4)."""
    for scope in (GRAPH_DEFAULT_SCOPE, sp_scope()):
        assert_test_tenant(str(redact_jwt(get_app_token(scope)).get("tid", "")))


@contextmanager
def open_env(spec_path: Path, sku_part_number: str = "") -> Iterator[Env]:
    """Guard the tenant, open Graph and SharePoint clients, and yield an Env."""
    settings = get_settings()
    site_url = settings.site_url.strip()
    parts = urlsplit(site_url)
    if not site_url or parts.hostname != settings.sp_host.strip():
        raise GeneratorError("LEAKLAB_SITE_URL must be set and use the LEAKLAB_SP_HOST host")
    guard_test_tenant()
    spec = load_spec(spec_path)
    gen_settings = GenSettings(
        password=settings.persona_password.get_secret_value(),
        usage_location=settings.usage_location.strip().upper(),
        site_host=settings.sp_host.strip(),
        site_path=parts.path.rstrip("/"),
        sku_part_number=sku_part_number,
    )
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    with GraphClient() as gc, SharePointRest(site_url) as sp:
        yield Env(
            spec=spec,
            graph=GraphGateway(gc),
            sp=SharePointGateway(sp),
            settings=gen_settings,
            log=MutationLog(run_id=stamp, created_at=time.time()),
        )


def write_report(out_dir: Path, command: str, payload: dict[str, Any]) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = out_dir / f"generator_{command}_{stamp}.json"
    n = 1
    while path.exists():
        n += 1
        path = out_dir / f"generator_{command}_{stamp}_{n}.json"
    path.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
    return path


SPEC_OPT = typer.Option(ROOT / "tenant_spec.yaml", help="Path to tenant_spec.yaml.")
OUT_OPT = typer.Option(ROOT / "runs", help="Where reports are written (timestamped).")
SKU_OPT = typer.Option("", help="License SKU part number (needed if more than one is assignable).")


@app.command("plan")
def cmd_plan(spec_path: Path = SPEC_OPT) -> None:
    """Show what would be created. No network access."""
    plan = build_plan(load_spec(spec_path))
    console.print(f"container folder: {plan.container}")
    console.print(f"users ({len(plan.users)}): {[u.upn for u in plan.users]}")
    console.print(f"groups ({len(plan.groups)}): {[g.name for g in plan.groups]}")
    console.print(f"folders: {[f.path for f in plan.folders]}")
    docs = sum(len(f.docs) for f in plan.folders)
    console.print(f"documents: {docs} + {len(plan.special_files)} special file(s)")
    console.print(f"site-root grants: {[(g.ref, g.role_name) for g in plan.root_grants]}")
    for item in plan.skipped:
        console.print(f"[yellow]skipped: {item}[/yellow]")


@app.command("generate")
def cmd_generate(
    spec_path: Path = SPEC_OPT, out_dir: Path = OUT_OPT, sku_part_number: str = SKU_OPT
) -> None:
    """Create (or complete) the tenant. Safe to run again."""
    with open_env(spec_path, sku_part_number) as env:
        report = generate(env)
    path = write_report(out_dir, "generate", report.model_dump())
    console.print(Pretty(report.counts))
    for w in report.warnings:
        console.print(f"[yellow]warning: {w}[/yellow]")
    console.print(f"report: {path}")


@app.command("verify")
def cmd_verify(spec_path: Path = SPEC_OPT, out_dir: Path = OUT_OPT) -> None:
    """Compare the tenant with the spec (REST for structure, Graph for membership)."""
    with open_env(spec_path) as env:
        report = verify(env)
    path = write_report(out_dir, "verify", report.model_dump())
    console.print(f"checked: {report.checked}")
    for i in report.issues:
        colour = "red" if i.severity == "error" else "yellow"
        console.print(f"[{colour}]{i.severity}: {i.what}[/{colour}] {i.expected} {i.actual}")
    console.print("[green]OK[/green]" if report.ok else "[bold red]NOT OK[/bold red]")
    console.print(f"report: {path}")
    raise typer.Exit(code=0 if report.ok else 1)


@app.command("teardown")
def cmd_teardown(spec_path: Path = SPEC_OPT, out_dir: Path = OUT_OPT) -> None:
    """Remove everything this spec generated (prefix-named objects only)."""
    with open_env(spec_path) as env:
        report = teardown(env)
    path = write_report(out_dir, "teardown", report.model_dump())
    console.print(Pretty(report.counts))
    console.print(f"report: {path}")
