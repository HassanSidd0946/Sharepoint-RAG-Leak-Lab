"""Probe: how consistent are real reads in a STEADY state (no membership changes during polling)?

The revocation runs showed three regimes for the same user: every read allowed, every read denied,
and reads that look like coin flips (about 50% allowed, old and fresh tokens independent, even
after the membership was removed). Before any revocation timing can be trusted, this probe asks
whether reads are consistent when nothing changes.

Setup: two folders with unique permissions and one canary file each:
  - <prefix>sp-cons-user<tag>:  Read granted DIRECTLY to the user (no group involved);
  - <prefix>sp-cons-group<tag>: Read granted to group <prefix>probe-cons<tag>, and the user is
    added to that group once, before polling.
Then, with ONE delegated token for the user (acquired with use_cache=False), every --interval
seconds for --duration seconds (default 5s / 600s): read both files (Graph content, canary
checked) and ask SharePoint REST for the user's effective permissions on both folders.
Nothing is removed or changed while polling.

What to look at in the table: the share of allowed reads per folder, how often the answer flips
between consecutive reads, and SharePoint's own "can read" share. If the DIRECT-grant folder is
consistent and the GROUP folder is not, the inconsistency is in how group membership is applied; if
both flip, it is not about groups.

--throwaway-user NAME uses a new user <prefix>probe-<NAME> instead of p-plateng; --suffix NAME
gives the folders and group new names. --cleanup deletes the folders and the group (the
throwaway user only with --delete-users). Output: probes/out/consistency_probe.json.

Run:  uv run python probes/consistency_probe.py [--suffix s1] [--duration 600]
"""

import hashlib
import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import typer
from rich.console import Console
from rich.markup import escape
from rich.pretty import Pretty
from rich.table import Table

from access_probe import read_file_as, sign_in, wait_license_visible, wait_membership_stable
from graph_probe import add_with_retry
from leaklab.auth import GRAPH_DEFAULT_SCOPE, AuthError, get_app_token, sp_scope
from leaklab.config import assert_test_tenant, get_settings, redact_jwt, redact_payload
from leaklab.graph_client import GraphClient, GraphError
from leaklab.sp_rest import SharePointRest, SPRestError, folder_item_path
from leaklab.spec import load_spec, user_upn
from persona_probe import ProbeStop, choose_sku, ensure_user
from revocation_probe import classify, ensure_throwaway_user, sp_view
from sp_probe import (
    Ctx,
    Recorder,
    StepFailed,
    assert_scenario_name,
    break_inheritance,
    create_folder,
    delete_folder,
    delete_groups,
    delete_or_gone,
    ensure_group,
    ensure_principal,
    grant_read,
    group_principal,
    read_role_id,
    resolve_site_and_drive,
    upload_file,
)

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "probes" / "out"
USER_ID = "p-plateng"
CANARY_PREFIX = "CANARY-CON-"
RUN_LENGTH = 5  # consecutive allowed reads that count as "access has started"

console = Console()
app = typer.Typer(add_completion=False)


@dataclass
class Opts:
    duration: float
    interval: float
    stable_timeout: float
    license_timeout: float
    wait: float
    sku_part_number: str
    throwaway_user: str
    suffix: str


def out_file_for(report: dict[str, Any]) -> Path:
    name = (
        "consistency_probe_cleanup.json"
        if report["mode"] == "cleanup"
        else "consistency_probe.json"
    )
    return OUT_DIR / name


def save(report: dict[str, Any]) -> None:
    out_file = out_file_for(report)
    if report.get("status") != "ok" and out_file.exists():
        try:
            previous = json.loads(out_file.read_text(encoding="utf-8")).get("status")
        except (OSError, ValueError):
            previous = None
        if previous == "ok":  # never let a failed run overwrite a successful result
            out_file = out_file.with_name(out_file.stem + ".failed.json")
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    console.print(f"Saved redacted results to {out_file}")


def summarize(
    classes: list[str], times: list[float], sp_reads: list[bool | None]
) -> dict[str, Any]:
    """Share of allowed reads, flips between consecutive reads, first run of RUN_LENGTH allowed."""
    allowed = [c == "allowed" for c in classes]
    flips = sum(1 for a, b in zip(allowed, allowed[1:], strict=False) if a != b)
    first_run: float | None = None
    streak = 0
    for i, ok in enumerate(allowed):
        streak = streak + 1 if ok else 0
        if streak == RUN_LENGTH:
            first_run = times[i - RUN_LENGTH + 1]
            break
    longest = cur = 0
    for ok in allowed:
        cur = cur + 1 if ok else 0
        longest = max(longest, cur)
    sp_known = [x for x in sp_reads if x is not None]
    return {
        "reads": len(classes),
        "allowed": sum(allowed),
        "allowed_pct": round(100 * sum(allowed) / max(len(allowed), 1), 1),
        "flips": flips,
        "flip_pct": round(100 * flips / max(len(allowed) - 1, 1), 1),
        "first_run_of_5_allowed_s": first_run,
        "longest_allowed_run": longest,
        "sharepoint_can_read_pct": (
            round(100 * sum(1 for x in sp_known if x) / len(sp_known), 1) if sp_known else None
        ),
    }


def run_probe(
    rec: Recorder, gc: GraphClient, sp: SharePointRest, ctx: Ctx, opts: Opts, report: dict[str, Any]
) -> None:
    settings = get_settings()
    location = settings.usage_location.strip().upper()
    if not ctx.password or len(location) != 2 or not location.isalpha():
        raise ProbeStop(
            "LEAKLAB_PERSONA_PASSWORD and a 2-letter LEAKLAB_USAGE_LOCATION are required."
        )
    p = ctx.prefix
    tag = f"-{opts.suffix}" if opts.suffix else ""
    canary = CANARY_PREFIX + hashlib.sha256(f"{ctx.seed}|consistency".encode()).hexdigest()[:6]
    report.update(canary=canary, options=opts.__dict__)

    console.print("[bold]Setup[/bold]")
    _, drive_id, library_url = resolve_site_and_drive(rec, gc, ctx.host, ctx.site_path, report)
    sku_id = choose_sku(gc.list_subscribed_skus(), opts.sku_part_number, report)
    if opts.throwaway_user:
        user = ensure_throwaway_user(
            gc, ctx, opts.throwaway_user, location, sku_id, opts.wait, report
        )
    else:
        user = ensure_user(gc, ctx.spec, USER_ID, ctx.password, location, sku_id, report)
        user.pop("created_at_monotonic", None)
    user["license_wait"] = wait_license_visible(gc, user["object_id"], sku_id, opts.license_timeout)
    report["user"] = user

    group = ensure_group(gc, f"{p}probe-cons{tag}", ctx, opts.wait, report)
    read_id = read_role_id(rec, sp, report)
    folders: dict[str, dict[str, str]] = {}
    for key in ("user", "group"):
        name = f"{p}sp-cons-{key}{tag}"
        folder = create_folder(rec, gc, drive_id, name, ctx)
        file = upload_file(
            rec,
            gc,
            drive_id,
            str(folder["id"]),
            f"{p}cons-{key}{tag}.txt",
            f"LeakLab consistency test ({key} grant).\nReference code: {canary}\n",
        )
        item = folder_item_path(f"{library_url}/{name}")
        break_inheritance(rec, sp, item, f"break inheritance ({key} folder)")
        folders[key] = {"item": item, "file_id": str(file["id"])}

    user_principal = ensure_principal(
        rec, sp, user["upn"], "user", "The user must exist in the directory."
    )
    login = str(user_principal["LoginName"])
    grant_read(
        rec,
        sp,
        folders["user"]["item"],
        int(user_principal["Id"]),
        read_id,
        "grant Read to the USER",
    )
    grant_read(
        rec,
        sp,
        folders["group"]["item"],
        int(group_principal(rec, sp, group["id"], "group")["Id"]),
        read_id,
        "grant Read to the GROUP",
    )
    report["memberships"] = []
    add_with_retry(
        partial(gc.add_user_to_group, group["id"], user["object_id"]), "user in group", report
    )
    t_add = time.monotonic()
    stable = wait_membership_stable(
        gc, user["object_id"], {group["id"]}, True, opts.stable_timeout, 5.0
    )
    report["membership_stable"] = stable
    if not stable["stable"]:
        raise ProbeStop(f"Membership not stable within {opts.stable_timeout:.0f}s.")

    token = sign_in(user["upn"], allow_device_code=False, use_cache=False)
    console.print(f"[bold]Polling {opts.duration:.0f}s: one token, nothing changes[/bold]")
    series: list[dict[str, Any]] = []
    report["series"] = series
    start = time.monotonic()
    k = 0
    while True:
        time.sleep(max(0.0, start + k * opts.interval - time.monotonic()))
        row: dict[str, Any] = {}
        for key, f in folders.items():
            result = read_file_as(token, drive_id, f["file_id"], canary)
            row[key] = {
                "class": classify(result),
                "status": result.get("first_status"),
                "final": result.get("final_status"),
                "code": result.get("error_code"),
                "sp": sp_view(sp, f["item"], login).get("can_read"),
            }
        row["t"] = round(time.monotonic() - t_add, 1)
        series.append(row)
        if k % 6 == 0:
            console.print(
                f"  t={row['t']:6.1f}s since add  user-grant={row['user']['class']}"
                f"  group-grant={row['group']['class']}"
                f"  (SharePoint can read: user={row['user']['sp']}, group={row['group']['sp']})"
            )
        if time.monotonic() - start >= opts.duration:
            break
        k += 1

    report["summary"] = {
        key: summarize(
            [r[key]["class"] for r in series],
            [r["t"] for r in series],
            [r[key]["sp"] for r in series],
        )
        for key in folders
    }


def run_cleanup(
    rec: Recorder,
    gc: GraphClient,
    ctx: Ctx,
    delete_users: bool,
    opts: Opts,
    report: dict[str, Any],
) -> None:
    p = ctx.prefix
    tag = f"-{opts.suffix}" if opts.suffix else ""
    _, drive_id, _ = resolve_site_and_drive(rec, gc, ctx.host, ctx.site_path, report)
    for key in ("user", "group"):
        delete_folder(rec, gc, drive_id, f"{p}sp-cons-{key}{tag}", ctx, report)
    delete_groups(gc, [f"{p}probe-cons{tag}"], ctx, report)
    if delete_users:
        targets = [(USER_ID, user_upn(ctx.spec, USER_ID))]
        if opts.throwaway_user:
            name = f"{p}probe-{opts.throwaway_user}"
            assert_scenario_name(name, p)
            targets = [(name, f"{name}@{ctx.domain}")]
        for label, upn in targets:
            found = gc.get_user_by_upn(upn)
            if found is not None:
                delete_or_gone("user", label, partial(gc.delete_user, found["id"]), report)


def print_results(report: dict[str, Any]) -> None:
    table = Table(title="consistency_probe: steady-state reads (nothing changes while polling)")
    for col in (
        "Grant",
        "Reads",
        "Allowed %",
        "Flip %",
        "First 5 allowed in a row (s)",
        "Longest allowed run",
        "SharePoint can-read %",
    ):
        table.add_column(col, overflow="fold")
    for key, st in report.get("summary", {}).items():
        table.add_row(
            key,
            str(st["reads"]),
            str(st["allowed_pct"]),
            str(st["flip_pct"]),
            str(st["first_run_of_5_allowed_s"]),
            str(st["longest_allowed_run"]),
            str(st["sharepoint_can_read_pct"]),
        )
    console.print(table)


@app.command()
def main(
    cleanup: bool = typer.Option(False, "--cleanup", help="Delete the folders and the group."),
    delete_users: bool = typer.Option(
        False, "--delete-users", help="With --cleanup: also the user."
    ),
    duration: float = typer.Option(600.0, help="Seconds of polling."),
    interval: float = typer.Option(5.0, help="Seconds between read cycles."),
    stable_timeout: float = typer.Option(180.0, help="Max seconds to wait for stable membership."),
    license_timeout: float = typer.Option(300.0, help="Max seconds to wait for the license."),
    wait: float = typer.Option(15.0, help="Seconds to wait after creating the group/user."),
    sku_part_number: str = typer.Option("", help="License SKU part number (if not unique)."),
    throwaway_user: str = typer.Option(
        "", help="Use a NEW user <prefix>probe-<NAME> instead of p-plateng."
    ),
    suffix: str = typer.Option("", help="Append -<SUFFIX> to folder, file and group names."),
    spec_path: Path = typer.Option(ROOT / "tenant_spec.yaml", help="Path to tenant_spec.yaml."),
) -> None:
    report: dict[str, Any] = {
        "probe": "consistency_probe",
        "run_at": datetime.now(UTC).isoformat(),
        "status": "running",
        "mode": "cleanup" if cleanup else "probe",
    }
    rec = Recorder(report)
    try:
        settings = get_settings()
        spec = load_spec(spec_path)
        site_url = settings.site_url.strip()
        host = settings.sp_host.strip()
        parts = urlsplit(site_url)
        if not site_url or parts.hostname != host:
            raise ValueError("LEAKLAB_SITE_URL must be set and use the LEAKLAB_SP_HOST host.")
        ctx = Ctx(
            spec=spec,
            prefix=spec.naming.prefix,
            seed=spec.naming.seed,
            host=host,
            site_path=parts.path.rstrip("/"),
            site_url=site_url,
            domain=spec.naming.domain,
            password=settings.persona_password.get_secret_value(),
        )
        report.update(prefix=ctx.prefix, site_url=site_url)
        for scope in (GRAPH_DEFAULT_SCOPE, sp_scope()):  # safety guard first
            assert_test_tenant(str(redact_jwt(get_app_token(scope)).get("tid", "")))
        console.print("[green]Tenant guard passed (Graph and SharePoint tokens)[/green]")
        opts = Opts(
            duration=duration,
            interval=interval,
            stable_timeout=stable_timeout,
            license_timeout=license_timeout,
            wait=wait,
            sku_part_number=sku_part_number,
            throwaway_user=throwaway_user,
            suffix=suffix,
        )
        with GraphClient() as gc, SharePointRest(site_url) as sp:
            if cleanup:
                run_cleanup(rec, gc, ctx, delete_users, opts, report)
            else:
                run_probe(rec, gc, sp, ctx, opts, report)
    except (StepFailed, ProbeStop) as exc:
        report["status"] = "failed"
        if isinstance(exc, StepFailed):
            report["failed_step"] = {
                "operation": exc.operation,
                "endpoint": exc.label,
                "http_status": exc.status,
                "body": redact_payload(exc.body),
                "suggested_next_probe": exc.hint,
            }
            detail: dict[str, Any] = {
                "what": exc.label,
                "HTTP status": exc.status,
                "body": redact_payload(exc.body),
                "hint": exc.hint,
            }
        else:
            report["stopped"] = {"message": str(exc), **exc.detail}
            detail = exc.detail
        console.print(f"[bold red]STOPPED:[/bold red] {escape(str(exc))}")
        for key, value in detail.items():
            console.print(f"{key}: ", end="")
            console.print(Pretty(value))
        save(report)
        raise typer.Exit(code=1) from exc
    except (
        AuthError,
        GraphError,
        SPRestError,
        RuntimeError,
        ValueError,
        OSError,
        httpx.HTTPError,
    ) as exc:
        report["status"] = "failed"
        report["error"] = f"{type(exc).__name__}: {exc}"
        console.print(f"[bold red]FAILED:[/bold red] {escape(report['error'])}")
        save(report)
        raise typer.Exit(code=1) from exc

    report["status"] = "ok"
    print_results(report)
    save(report)


if __name__ == "__main__":
    app()
