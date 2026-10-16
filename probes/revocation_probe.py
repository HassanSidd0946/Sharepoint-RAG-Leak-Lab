"""Probe: how long after a membership is REMOVED does a real file read turn from 200 to 403?

Scenario S2 (group removal). Every read is a real one: Graph GET /drives/{d}/items/{id}/content
with a DELEGATED token for p-plateng, canary checked in the body.

Setup (once): p-plateng (licensed persona), group <prefix>probe-revoc, folder
<prefix>sp-revoc-folder with <prefix>revoc.txt (canary), inheritance broken, Read granted to the
group ONLY. --mode direct (default): p-plateng is a direct member of the group; the removal takes
p-plateng out of it. --mode nested: p-plateng is in <prefix>probe-revoc-inner, which is nested in
the group; the removal takes the INNER GROUP out of the outer one (S2 as in the spec).

Each trial (--trials, default 2):
  1. add the membership (nested: the inner group into the outer one);
  2. wait for stability: two consistent transitiveMemberOf reads, then a 200 read with a token
     acquired for this trial (the "old" token); the seconds from the add to that first 200 are
     recorded (how long a new member waits for access);
  3. remove the membership (the clock T0 starts when the DELETE returns);
  4. poll reads every --interval seconds (default 2) with the OLD token (acquired before the
     removal, same token the whole time) and, unless --tokens old, with a FRESH token fetched
     right after the removal (use_cache=False, so it is really new);
  5. a token counts as denied when a read gives 403/404; "stable denial" = 3 reads in a row.
     Stops when every polled token has a stable denial, or after --poll-timeout seconds.
App-only (driver) control reads before and after prove the file still exists.

Besides the Graph reads, every read cycle also asks SharePoint REST for the user's EFFECTIVE
permissions on the folder (no token involved): that separates "SharePoint thinks the user
can read" from "something in the Graph content path denies". --suffix NAME gives the
folder, file and groups new names (the run that worked was the first to use its names).

Caveat: polling both tokens for the same user in the same trial could influence each other (e.g.
caches); use --tokens old / --tokens fresh in separate runs to compare without that.

--cleanup deletes the folder and the probe groups (the persona user only with --delete-users).
Output: rich table + probes/out/revocation_probe.json (redacted: no tokens, no URLs).

Run:  uv run python probes/revocation_probe.py [--mode nested] [--trials 2] [--tokens both]
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
from leaklab.sp_rest import SharePointRest, SPRestError, folder_item_path, parse_body
from leaklab.spec import load_spec, user_upn
from persona_probe import ProbeStop, choose_sku, ensure_user
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
    snapshot,
    upload_file,
)

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "probes" / "out"
USER_ID = "p-plateng"
STABLE_DENIALS = 3  # consecutive denied reads that count as a "stable denial"
CANARY_PREFIX = "CANARY-REV-"

console = Console()
app = typer.Typer(add_completion=False)


@dataclass
class Opts:
    mode: str
    tokens: str
    trials: int
    interval: float
    poll_timeout: float
    stable_timeout: float
    access_timeout: float
    license_timeout: float
    wait: float
    sku_part_number: str
    entra_check_interval: float = 30.0
    throwaway_user: str = ""
    suffix: str = ""


def out_file_for(report: dict[str, Any]) -> Path:
    """direct: revocation_probe.json; nested: ..._nested.json; cleanup: ..._cleanup.json."""
    if report["mode"] == "cleanup":
        return OUT_DIR / "revocation_probe_cleanup.json"
    variant = report.get("variant", "direct")
    return OUT_DIR / (
        "revocation_probe.json" if variant == "direct" else f"revocation_probe_{variant}.json"
    )


def save(report: dict[str, Any]) -> None:
    out_file = out_file_for(report)
    if report.get("status") != "ok" and out_file.exists():
        # Never let a failed run overwrite a successful result (this lost data once).
        try:
            previous = json.loads(out_file.read_text(encoding="utf-8")).get("status")
        except (OSError, ValueError):
            previous = None
        if previous == "ok":
            out_file = out_file.with_name(out_file.stem + ".failed.json")
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    console.print(f"Saved redacted results to {out_file}")


def classify(result: dict[str, Any]) -> str:
    """allowed (200 + canary), denied (403/404), or error (anything else: 401, 429, 5xx...)."""
    if result.get("final_status") == 200 and result.get("canary_found"):
        return "allowed"
    if result.get("first_status") in (403, 404) or result.get("final_status") in (403, 404):
        return "denied"
    return "error"


def new_state() -> dict[str, Any]:
    return {
        "reads": 0,
        "classes": {"allowed": 0, "denied": 0, "error": 0},
        "streak": 0,
        "run_start_s": None,
        "first_denied_s": None,
        "stable_denied_s": None,
        "flapped": False,
        "seen_denied": False,
        "last_status": None,
    }


def update_state(state: dict[str, Any], cls: str, t: float, status: Any) -> None:
    state["reads"] += 1
    state["classes"][cls] += 1
    state["last_status"] = status
    if cls == "denied":
        state["seen_denied"] = True
        state["streak"] += 1
        if state["first_denied_s"] is None:
            state["first_denied_s"] = t
        if state["streak"] == 1:
            state["run_start_s"] = t
        if state["streak"] >= STABLE_DENIALS and state["stable_denied_s"] is None:
            state["stable_denied_s"] = state["run_start_s"]
    else:
        if cls == "allowed" and state["seen_denied"]:
            state["flapped"] = True  # access came BACK after a denial
        state["streak"] = 0


def read_once(token: str, drive_id: str, file_id: str, canary: str) -> tuple[str, dict[str, Any]]:
    result = read_file_as(token, drive_id, file_id, canary)
    return classify(result), result


# BasePermissions.Low bits: ViewListItems (bit 0) and OpenItems (bit 5) are what Read grants.
VIEW_AND_OPEN_MASK = 0x1 | 0x20


def sp_view(sp: SharePointRest, item_path: str, login: str) -> dict[str, Any]:
    """SharePoint's own effective-permission answer for the user (no token involved)."""
    try:
        data = parse_body(sp.get_user_effective_permissions(item_path, login))
        if isinstance(data, dict) and isinstance(data.get("value"), dict):
            data = data["value"]
        if isinstance(data, dict) and isinstance(data.get("GetUserEffectivePermissions"), dict):
            data = data["GetUserEffectivePermissions"]
        low = int(data["Low"])
    except (SPRestError, KeyError, TypeError, ValueError) as exc:
        return {"error": f"{type(exc).__name__}: {str(exc)[:200]}"}
    return {"low": low, "can_read": (low & VIEW_AND_OPEN_MASK) == VIEW_AND_OPEN_MASK}


def poll_after_removal(
    t0: float,
    old_token: str,
    upn: str,
    drive_id: str,
    file_id: str,
    canary: str,
    opts: Opts,
    gc: GraphClient,
    user_id: str,
    outer_id: str,
    sp: SharePointRest,
    item_path: str,
    login: str,
) -> dict[str, Any]:
    """Read every `interval` seconds after the removal; return the series and per-token summary.

    Control: every `entra_check_interval` seconds, ask Graph whether the user is still
    (transitively) in the granted group, so a 200 can be told apart from "the removal never
    took effect".
    """
    use_old = opts.tokens in ("both", "old")
    use_fresh = opts.tokens in ("both", "fresh")
    tokens: dict[str, str] = {"old": old_token} if use_old else {}
    states: dict[str, dict[str, Any]] = {name: new_state() for name in tokens}
    series: list[dict[str, Any]] = []
    fresh_info: dict[str, Any] | None = None
    last_print = -999.0
    entra_checks: list[dict[str, Any]] = []
    last_entra = -999.0
    entra_removed_s: float | None = None
    sp_not_readable_s: float | None = None
    k = 0
    while True:
        due = t0 + k * opts.interval
        time.sleep(max(0.0, due - time.monotonic()))
        row: dict[str, Any] = {}
        for name in list(tokens):
            cls, result = read_once(tokens[name], drive_id, file_id, canary)
            t = round(time.monotonic() - t0, 1)
            row[name] = {
                "class": cls,
                "status": result.get("first_status"),
                "code": result.get("error_code"),
            }
            update_state(states[name], cls, t, result.get("first_status"))
        if k == 0 and use_fresh:
            started = time.monotonic() - t0
            token = sign_in(upn, allow_device_code=False, use_cache=False)
            tokens["fresh"] = token
            states["fresh"] = new_state()
            fresh_info = {
                "fetch_started_s": round(started, 1),
                "acquired_at_s": round(time.monotonic() - t0, 1),
            }
            cls, result = read_once(token, drive_id, file_id, canary)
            t = round(time.monotonic() - t0, 1)
            row["fresh"] = {
                "class": cls,
                "status": result.get("first_status"),
                "code": result.get("error_code"),
            }
            update_state(states["fresh"], cls, t, result.get("first_status"))
        row["sp"] = sp_view(sp, item_path, login)
        row["t"] = round(time.monotonic() - t0, 1)
        if row["sp"].get("can_read") is False and sp_not_readable_s is None:
            sp_not_readable_s = row["t"]
        series.append(row)
        if row["t"] - last_entra >= opts.entra_check_interval or k == 0:
            last_entra = row["t"]
            still = outer_id in {g["id"] for g in gc.list_transitive_member_of(user_id)}
            entra_checks.append({"t": row["t"], "still_member": still})
            if not still and entra_removed_s is None:
                entra_removed_s = row["t"]
            console.print(f"  [dim]Entra still lists the user in the group: {still}[/dim]")
        if row["t"] - last_print >= 15 or k == 0:
            last_print = row["t"]
            shown = "  ".join(f"{n}={row[n]['class']}({row[n]['status']})" for n in tokens)
            shown += f"  SharePoint says can read: {row['sp'].get('can_read')}"
            console.print(f"  t={row['t']:6.1f}s  {shown}")
        if all(s["stable_denied_s"] is not None for s in states.values()):
            break
        if time.monotonic() - t0 >= opts.poll_timeout:
            console.print(
                "[yellow]poll timeout reached before every token was stably denied[/yellow]"
            )
            break
        k += 1
    cycles = max(len(series) - 1, 1)
    return {
        "series": series,
        "summary": states,
        "fresh_token": fresh_info,
        "entra_checks": entra_checks,
        "entra_first_seen_removed_s": entra_removed_s,
        "sp_first_not_readable_s": sp_not_readable_s,
        "achieved_cycle_seconds": round(series[-1]["t"] / cycles, 1),
    }


def run_trial(
    n: int,
    gc: GraphClient,
    *,
    opts: Opts,
    groups: dict[str, dict[str, Any]],
    user: dict[str, Any],
    drive_id: str,
    file_id: str,
    canary: str,
    report: dict[str, Any],
    sp: SharePointRest,
    item_path: str,
    login: str,
) -> dict[str, Any]:
    console.print(f"[bold]Trial {n}[/bold]")
    trial: dict[str, Any] = {"trial": n}
    report["trials"].append(trial)
    outer = groups["outer"]["id"]
    if opts.mode == "nested":
        inner = groups["inner"]["id"]
        if n == 1:
            add_with_retry(
                lambda: gc.add_user_to_group(inner, user["object_id"]), "user in inner", report
            )
        link_what = "inner group in outer"
        add_link = partial(gc.add_group_to_group, outer, inner)
        remove_link = partial(gc.remove_group_member, outer, inner)
    else:
        link_what = "user in outer"
        add_link = partial(gc.add_user_to_group, outer, user["object_id"])
        remove_link = partial(gc.remove_group_member, outer, user["object_id"])

    console.print(f"  adding membership ({link_what})")
    add_with_retry(add_link, link_what, report)
    t_add = time.monotonic()

    console.print("  waiting for stable membership")
    stable = wait_membership_stable(gc, user["object_id"], {outer}, True, opts.stable_timeout, 5.0)
    trial["membership_stable"] = stable
    if not stable["stable"]:
        raise ProbeStop(f"Membership not stable within {opts.stable_timeout:.0f}s.", {"trial": n})

    old_token = sign_in(user["upn"], allow_device_code=False, use_cache=False)
    console.print("  waiting for the first 200 read (new member access)")
    waits: list[dict[str, Any]] = []
    trial["access_waits"] = waits
    while True:
        cls, result = read_once(old_token, drive_id, file_id, canary)
        elapsed = round(time.monotonic() - t_add, 1)
        entra_member = outer in {g["id"] for g in gc.list_transitive_member_of(user["object_id"])}
        sp_state = sp_view(sp, item_path, login)
        waits.append(
            {
                "since_add_s": elapsed,
                "class": cls,
                "status": result.get("first_status"),
                "code": result.get("error_code"),
                "entra_lists_member": entra_member,
                "sp_view": sp_state,
            }
        )
        console.print(
            f"    t={elapsed:6.1f}s since add: {cls} ({result.get('first_status')}); "
            f"Entra lists member: {entra_member}; "
            f"SharePoint says can read: {sp_state.get('can_read')}"
        )
        if cls == "allowed":
            trial["seconds_from_add_to_first_200"] = elapsed
            break
        if time.monotonic() - t_add > opts.access_timeout:
            raise ProbeStop(
                f"No 200 read within {opts.access_timeout:.0f}s of the add.", {"trial": n}
            )
        time.sleep(5.0)
    trial["access_waits"] = waits
    trial["control_before"] = classify(
        read_file_as(get_app_token(GRAPH_DEFAULT_SCOPE), drive_id, file_id, canary)
    )

    console.print(f"  [bold]REMOVING membership ({link_what})[/bold]")
    t_call = time.monotonic()
    try:
        remove_link()
    except GraphError as exc:
        raise ProbeStop("Removing the membership failed.", {"error": str(exc)}) from exc
    t0 = time.monotonic()
    trial["removal_call_seconds"] = round(t0 - t_call, 2)
    trial["removed_at"] = datetime.now(UTC).isoformat()

    polled = poll_after_removal(
        t0,
        old_token,
        user["upn"],
        drive_id,
        file_id,
        canary,
        opts,
        gc,
        user["object_id"],
        outer,
        sp,
        item_path,
        login,
    )
    trial.update(polled)
    trial["control_after"] = classify(
        read_file_as(get_app_token(GRAPH_DEFAULT_SCOPE), drive_id, file_id, canary)
    )
    return trial


def ensure_throwaway_user(
    gc: GraphClient,
    ctx: Ctx,
    short: str,
    location: str,
    sku_id: str,
    wait_s: float,
    report: dict[str, Any],
) -> dict[str, Any]:
    """A user named <prefix>probe-<short> that has (by design) never touched SharePoint.

    Used to test whether SharePoint's per-user view of group membership is set when a user first
    accesses it and then kept. Use a NEW name each run: an existing user is not fresh.
    """
    name = f"{ctx.prefix}probe-{short}"
    assert_scenario_name(name, ctx.prefix)
    upn = f"{name}@{ctx.domain}"
    found = gc.get_user_by_upn(upn)
    state = "reused"
    if found is None:
        found = gc.create_user(upn, name, name, ctx.password, location)
        gc.assign_license(found["id"], sku_id)
        state = "created"
        console.print(f"  throwaway user {upn}: created; waiting {wait_s:.0f}s to replicate")
        time.sleep(wait_s)
    else:
        console.print(
            f"[yellow]  throwaway user {upn} already exists: NOT fresh, it may have accessed "
            "SharePoint before. Use a new --throwaway-user name.[/yellow]"
        )
    return {"upn": upn, "object_id": found["id"], "state": state, "throwaway": True}


def run_probe(
    rec: Recorder,
    gc: GraphClient,
    sp: SharePointRest,
    ctx: Ctx,
    opts: Opts,
    report: dict[str, Any],
) -> None:
    settings = get_settings()
    location = settings.usage_location.strip().upper()
    if not ctx.password or len(location) != 2 or not location.isalpha():
        raise ProbeStop(
            "LEAKLAB_PERSONA_PASSWORD and a 2-letter LEAKLAB_USAGE_LOCATION are required."
        )
    p = ctx.prefix
    canary = CANARY_PREFIX + hashlib.sha256(f"{ctx.seed}|revocation".encode()).hexdigest()[:6]
    report.update(canary=canary, options=opts.__dict__)
    tag = f"-{opts.suffix}" if opts.suffix else ""

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

    report["memberships"] = []
    groups: dict[str, dict[str, Any]] = {
        "outer": ensure_group(gc, f"{p}probe-revoc{tag}", ctx, opts.wait, report)
    }
    if opts.mode == "nested":
        groups["inner"] = ensure_group(gc, f"{p}probe-revoc-inner{tag}", ctx, 0.0, report)

    folder_name, file_name = f"{p}sp-revoc-folder{tag}", f"{p}revoc{tag}.txt"
    folder = create_folder(rec, gc, drive_id, folder_name, ctx)
    file = upload_file(
        rec,
        gc,
        drive_id,
        str(folder["id"]),
        file_name,
        f"LeakLab revocation test document.\nReference code: {canary}\n",
    )
    folder_item = folder_item_path(f"{library_url}/{folder_name}")
    break_inheritance(rec, sp, folder_item, "break inheritance (folder)")
    principal = group_principal(rec, sp, groups["outer"]["id"], "revocation group")
    grant_read(
        rec,
        sp,
        folder_item,
        int(principal["Id"]),
        read_role_id(rec, sp, report),
        "grant Read to the revocation group ONLY",
    )
    snapshot(rec, sp, "folder after grant", folder_item, report)
    user_login = str(
        ensure_principal(
            rec,
            sp,
            user["upn"],
            "user (SharePoint principal)",
            "The user must exist in the directory.",
        )["LoginName"]
    )
    report["user_login"] = user_login
    report["ids"] = {"drive_id": drive_id, "folder_id": folder["id"], "file_id": file["id"]}

    report["trials"] = []
    for n in range(1, opts.trials + 1):
        run_trial(
            n,
            gc,
            opts=opts,
            groups=groups,
            user=user,
            drive_id=drive_id,
            file_id=str(file["id"]),
            canary=canary,
            report=report,
            sp=sp,
            item_path=folder_item,
            login=user_login,
        )


def run_cleanup(
    rec: Recorder,
    gc: GraphClient,
    sp: SharePointRest,
    ctx: Ctx,
    delete_users: bool,
    throwaway: str,
    suffix: str,
    report: dict[str, Any],
) -> None:
    p = ctx.prefix
    tag = f"-{suffix}" if suffix else ""
    _, drive_id, _ = resolve_site_and_drive(rec, gc, ctx.host, ctx.site_path, report)
    delete_folder(rec, gc, drive_id, f"{p}sp-revoc-folder{tag}", ctx, report)
    delete_groups(gc, [f"{p}probe-revoc{tag}", f"{p}probe-revoc-inner{tag}"], ctx, report)
    if delete_users:
        targets = [(USER_ID, user_upn(ctx.spec, USER_ID))]
        if throwaway:  # only the named throwaway user, never p-plateng
            name = f"{p}probe-{throwaway}"
            assert_scenario_name(name, p)
            targets = [(name, f"{name}@{ctx.domain}")]
        for label, upn in targets:
            found = gc.get_user_by_upn(upn)
            if found is not None:
                delete_or_gone("user", label, partial(gc.delete_user, found["id"]), report)


def print_results(report: dict[str, Any]) -> None:
    table = Table(title="revocation_probe: seconds after removal until reads are denied")
    for col in (
        "Trial",
        "Token",
        "First 200 after add (s)",
        "First 403 (s)",
        "Stable denial (s)",
        "Reads (ok/denied/err)",
        "Came back?",
    ):
        table.add_column(col, overflow="fold")
    for trial in report.get("trials", []):
        for name, st in trial.get("summary", {}).items():
            c = st["classes"]
            table.add_row(
                str(trial["trial"]),
                name,
                str(trial.get("seconds_from_add_to_first_200")),
                str(st["first_denied_s"]),
                str(st["stable_denied_s"]),
                f"{c['allowed']}/{c['denied']}/{c['error']}",
                "YES" if st["flapped"] else "no",
            )
    console.print(table)
    for trial in report.get("trials", []):
        if "achieved_cycle_seconds" not in trial:
            continue  # the trial stopped before polling started
        console.print(
            f"trial {trial['trial']}: Entra first reported the user removed at "
            f"t={trial.get('entra_first_seen_removed_s')}s; one read cycle took about "
            f"{trial.get('achieved_cycle_seconds')}s (asked for {report['options']['interval']}s)"
        )
        fresh = trial.get("fresh_token")
        if fresh:
            console.print(
                f"trial {trial['trial']}: fresh token fetched at t={fresh['acquired_at_s']}s "
                f"(removal call took {trial['removal_call_seconds']}s; control reads "
                f"{trial['control_before']} -> {trial['control_after']})"
            )


@app.command()
def main(
    cleanup: bool = typer.Option(False, "--cleanup", help="Delete the folder and probe groups."),
    delete_users: bool = typer.Option(
        False, "--delete-users", help="With --cleanup: also p-plateng."
    ),
    mode: str = typer.Option("direct", help="'direct' (user removed from the group) or 'nested'."),
    tokens: str = typer.Option("both", help="'both', 'old' or 'fresh' tokens polled per trial."),
    trials: int = typer.Option(2, help="Complete add/remove cycles."),
    interval: float = typer.Option(2.0, help="Seconds between polling reads."),
    poll_timeout: float = typer.Option(900.0, help="Max seconds to poll after a removal."),
    stable_timeout: float = typer.Option(180.0, help="Max seconds to wait for stable membership."),
    access_timeout: float = typer.Option(300.0, help="Max seconds to wait for the first 200."),
    license_timeout: float = typer.Option(300.0, help="Max seconds to wait for the license."),
    wait: float = typer.Option(15.0, help="Seconds to wait after creating the group."),
    sku_part_number: str = typer.Option("", help="License SKU part number (if not unique)."),
    suffix: str = typer.Option(
        "", help="Append -<SUFFIX> to folder, file and group names (new names rule out reuse)."
    ),
    throwaway_user: str = typer.Option(
        "",
        help="Use a NEW user <prefix>probe-<NAME> instead of p-plateng (never touched SharePoint).",
    ),
    spec_path: Path = typer.Option(ROOT / "tenant_spec.yaml", help="Path to tenant_spec.yaml."),
) -> None:
    if mode not in ("direct", "nested") or tokens not in ("both", "old", "fresh"):
        raise typer.BadParameter("--mode: direct|nested; --tokens: both|old|fresh")
    report: dict[str, Any] = {
        "probe": "revocation_probe",
        "variant": mode,
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
        report.update(prefix=ctx.prefix, site_url=site_url, scenario=f"revocation ({mode})")

        for scope in (GRAPH_DEFAULT_SCOPE, sp_scope()):  # safety guard first
            assert_test_tenant(str(redact_jwt(get_app_token(scope)).get("tid", "")))
        console.print("[green]Tenant guard passed (Graph and SharePoint tokens)[/green]")

        opts = Opts(
            mode=mode,
            tokens=tokens,
            trials=trials,
            interval=interval,
            poll_timeout=poll_timeout,
            stable_timeout=stable_timeout,
            access_timeout=access_timeout,
            license_timeout=license_timeout,
            wait=wait,
            sku_part_number=sku_part_number,
            throwaway_user=throwaway_user,
            suffix=suffix,
        )
        with GraphClient() as gc, SharePointRest(site_url) as sp:
            if cleanup:
                run_cleanup(
                    rec, gc, sp, ctx, delete_users, opts.throwaway_user, opts.suffix, report
                )
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
        print_results(report)
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
