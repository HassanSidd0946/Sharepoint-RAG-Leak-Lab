"""Probe: how do real reads behave under three different grant types, in steady state and after
a simultaneous revocation? (user p-plateng, one folder and one canary file per arm)

The revocation runs showed three regimes for the same user: every read allowed, every read denied,
and coin-flip reads (about 50% allowed), and a revocation that was not honored for 15-30 minutes.
This probe asks whether that depends on HOW access was granted.

Arms (each: its own folder with broken inheritance, ONE canary file, nothing else granted):
  direct   Read granted directly to the user (i:0#.f|membership|<upn>);
  spgroup  Read granted to a NEW SharePoint group that contains the user directly;
  entra    Read granted to an Entra security group that contains the user.

Phase 1 - steady state (--phase1, default 600s): one delegated token for the user, every
  --interval seconds read every arm (Graph content, canary checked) and ask SharePoint REST for
  the user's effective permissions on each arm's folder. Nothing changes. Per arm: allowed %, flip
  % (answer differs from the previous read), first run of 5 allowed in a row.
Between the phases: an arm that never showed 5 allowed reads in a row has nothing to revoke; it
  gets --baseline-wait seconds (default 300) more, and if it still shows no access it is flagged
  "NO (nothing to revoke)" and left out of the stop condition and the conclusions.
Phase 2 - revocation (--phase2, default 2700s = 45 min): a token is acquired seconds before t0
  (the "pre-t0" token); then the user is removed from every arm back to back (direct: the role
  assignment is removed; spgroup: the user is removed from the SharePoint group; entra: the user
  is removed from the Entra group). Right after, a FRESH token is acquired. Both tokens read every
  arm every cycle until every arm and token has 10 denials in a row, or the time is up. Each arm
  is timed from the moment ITS removal returned (the three removals take a few seconds in total
  and are recorded). Per arm and token: time to first denial, time at which 10 denials in a row
  began, number of allowed reads after t0, and whether access came back after a denial. Every
  60s the probe also checks the structure itself (Entra membership, SharePoint group
  membership, the direct assignment) so a removal that did not happen is visible.

Output: probes/out/consistency_probe_run_<UTC timestamp>.json (cleanup:
consistency_probe_cleanup_<timestamp>.json). A new file per run, never an existing one; that
same file is rewritten as a checkpoint during the run (status "running") and on Ctrl-C (status
"interrupted"). Redacted: no tokens, passwords or URLs. A full run takes up to about an hour.

--throwaway-user NAME uses a new user <prefix>probe-<NAME> instead of p-plateng; --suffix NAME gives
the folders and groups new names. --cleanup (same --suffix) deletes the folders and both groups.

Run:  uv run python probes/consistency_probe.py [--suffix c1]
"""

import hashlib
import json
import time
from collections.abc import Callable
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

from access_probe import sign_in, wait_license_visible, wait_membership_stable
from graph_probe import add_with_retry
from leaklab.auth import GRAPH_DEFAULT_SCOPE, AuthError, get_app_token, sp_scope
from leaklab.config import assert_test_tenant, get_settings, redact_jwt, redact_payload
from leaklab.graph_client import GraphClient, GraphError
from leaklab.sp_rest import SharePointRest, SPRestError, folder_item_path
from leaklab.spec import load_spec, user_upn
from persona_probe import ProbeStop, choose_sku, ensure_user
from revocation_probe import (
    SETTLED_DENIALS,
    ensure_throwaway_user,
    entry,
    new_state,
    read_once,
    sp_view,
    update_state,
)
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
    step,
    upload_file,
)

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "probes" / "out"
USER_ID = "p-plateng"
ARMS = ("direct", "spgroup", "entra")
CANARY_PREFIX = "CANARY-CON-"
RUN_LENGTH = 5  # consecutive allowed reads that count as "access has started"
STRUCTURE_CHECK_S = 60.0
CHECKPOINT_S = 300.0

console = Console()
app = typer.Typer(add_completion=False)


@dataclass
class Opts:
    phase1: float
    phase2: float
    interval: float
    stable_timeout: float
    license_timeout: float
    wait: float
    sku_part_number: str
    throwaway_user: str
    suffix: str
    baseline_wait: float = 300.0


class Saver:
    """One timestamped output file per run; never an existing file. Rewritten as a checkpoint."""

    def __init__(self, kind: str) -> None:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        path = OUT_DIR / f"consistency_probe_{kind}_{stamp}.json"
        n = 1
        while True:
            try:
                path.open("x", encoding="utf-8").close()  # reserve; fails if it exists
                break
            except FileExistsError:
                n += 1
                path = OUT_DIR / f"consistency_probe_{kind}_{stamp}_{n}.json"
        self.path = path

    def write(self, report: dict[str, Any]) -> None:
        self.path.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")


def summarize(
    classes: list[str], times: list[float], sp_reads: list[bool | None]
) -> dict[str, Any]:
    """Allowed %, flips between consecutive reads, first run of RUN_LENGTH allowed reads."""
    allowed = [c == "allowed" for c in classes]
    flips = sum(1 for a, b in zip(allowed, allowed[1:], strict=False) if a != b)
    first_run: float | None = None
    streak = longest = 0
    for i, ok in enumerate(allowed):
        streak = streak + 1 if ok else 0
        longest = max(longest, streak)
        if streak == RUN_LENGTH and first_run is None:
            first_run = times[i - RUN_LENGTH + 1]
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


def setup_arms(
    rec: Recorder,
    gc: GraphClient,
    sp: SharePointRest,
    ctx: Ctx,
    opts: Opts,
    user: dict[str, Any],
    drive_id: str,
    library_url: str,
    canary: str,
    report: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """Create the three folders and grants, put the user in both groups, wait for stability."""
    p = ctx.prefix
    tag = f"-{opts.suffix}" if opts.suffix else ""
    read_id = read_role_id(rec, sp, report)
    user_principal = ensure_principal(
        rec, sp, user["upn"], "user", "The user must exist in the directory."
    )
    arms: dict[str, dict[str, Any]] = {
        "user_login": {
            "login": str(user_principal["LoginName"]),
            "sp_id": int(user_principal["Id"]),
        }
    }
    for arm in ARMS:
        name = f"{p}sp-cons-{arm}{tag}"
        folder = create_folder(rec, gc, drive_id, name, ctx)
        file = upload_file(
            rec,
            gc,
            drive_id,
            str(folder["id"]),
            f"{p}cons-{arm}{tag}.txt",
            f"LeakLab consistency test ({arm} arm).\nReference code: {canary}\n",
        )
        item = folder_item_path(f"{library_url}/{name}")
        break_inheritance(rec, sp, item, f"break inheritance ({arm} folder)")
        arms[arm] = {"item": item, "file_id": str(file["id"]), "folder_id": str(folder["id"])}

    # ARM direct: the user itself
    grant_read(
        rec,
        sp,
        arms["direct"]["item"],
        arms["user_login"]["sp_id"],
        read_id,
        "DIRECT arm: grant Read to the user",
    )

    # ARM spgroup: a new SharePoint group that contains the user
    spg_title = f"{p}probe-cons-spg{tag}"
    assert_scenario_name(spg_title, p)
    _, found = step(
        rec,
        "find SharePoint group",
        "GET /_api/web/sitegroups?$filter=Title eq '<title>'",
        lambda: sp.find_site_group(spg_title),
        lambda d: f"{len(d.get('value', []))} found",
    )
    if found.get("value"):
        raise StepFailed(
            "find SharePoint group",
            f"{spg_title!r} already exists",
            0,
            None,
            "Run --cleanup with the same --suffix, or use a new --suffix.",
        )
    _, spg = step(
        rec,
        "create SharePoint group",
        "POST /_api/web/sitegroups",
        lambda: sp.create_site_group(spg_title, "LeakLab consistency probe"),
        lambda d: f"Id={d.get('Id')}",
        "UNVERIFIED endpoint; read the error body. Next probe: create the group by hand in the UI.",
    )
    spg_id = int(spg["Id"])
    step(
        rec,
        "add user to SharePoint group",
        "POST /_api/web/sitegroups/getbyid(<id>)/users",
        lambda: sp.add_user_to_site_group(spg_id, arms["user_login"]["login"]),
        lambda d: f"added; Id={d.get('Id') if isinstance(d, dict) else None}",
        "UNVERIFIED endpoint; read the error body.",
    )
    grant_read(
        rec, sp, arms["spgroup"]["item"], spg_id, read_id, "SPGROUP arm: grant Read to the group"
    )
    arms["spgroup"]["group_id"] = spg_id
    arms["spgroup"]["title"] = spg_title

    # ARM entra: an Entra security group that contains the user
    entra = ensure_group(gc, f"{p}probe-cons-ent{tag}", ctx, opts.wait, report)
    entra_principal = group_principal(rec, sp, entra["id"], "entra arm group")
    grant_read(
        rec, sp, arms["entra"]["item"], int(entra_principal["Id"]), read_id, "ENTRA arm: grant Read"
    )
    report["memberships"] = []
    add_with_retry(
        partial(gc.add_user_to_group, entra["id"], user["object_id"]), "user in Entra group", report
    )
    arms["entra"]["group_id"] = entra["id"]
    stable = wait_membership_stable(
        gc, user["object_id"], {entra["id"]}, True, opts.stable_timeout, 5.0
    )
    report["membership_stable"] = stable
    if not stable["stable"]:
        raise ProbeStop(f"Entra membership not stable within {opts.stable_timeout:.0f}s.")
    return arms


def structure_check(
    gc: GraphClient, sp: SharePointRest, user: dict[str, Any], arms: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """Is the user still in the Entra group / the SharePoint group / assigned on the folder?"""
    login = arms["user_login"]["login"]
    out: dict[str, Any] = {}
    try:
        ids = {g["id"] for g in gc.list_transitive_member_of(user["object_id"])}
        out["entra_member"] = arms["entra"]["group_id"] in ids
    except (GraphError, httpx.TransportError) as exc:
        out["entra_member"] = f"error: {getattr(exc, 'status_code', type(exc).__name__)}"
    try:
        users = parse_users(sp.get_group_users(arms["spgroup"]["group_id"]))
        out["spgroup_member"] = login in users
    except (SPRestError, ValueError, httpx.TransportError) as exc:
        out["spgroup_member"] = f"error: {type(exc).__name__}"
    try:
        data = sp.get_role_assignments(arms["direct"]["item"]).json()
        logins = {a.get("Member", {}).get("LoginName") for a in data.get("value", [])}
        out["direct_assignment_present"] = login in logins
    except (SPRestError, ValueError, httpx.TransportError) as exc:
        out["direct_assignment_present"] = f"error: {type(exc).__name__}"
    return out


def parse_users(resp: httpx.Response) -> set[str]:
    data = resp.json()
    return {str(u.get("LoginName")) for u in data.get("value", [])}


def safe_read_once(
    token: str, drive_id: str, file_id: str, canary: str
) -> tuple[str, dict[str, Any]]:
    """A read that never raises on a network error: it becomes an "error" read and polling goes on.

    (One SSL handshake timeout ended a 39-minute observation once; reads are safe to repeat.)
    """
    try:
        return read_once(token, drive_id, file_id, canary)
    except httpx.TransportError as exc:
        return "error", {
            "first_status": None,
            "final_status": None,
            "error_code": f"transport:{type(exc).__name__}",
        }


def safe_sp_view(sp: SharePointRest, item_path: str, login: str) -> bool | None:
    try:
        return sp_view(sp, item_path, login).get("can_read")
    except httpx.TransportError:
        return None


def read_arms(
    tokens: dict[str, str],
    arms: dict[str, dict[str, Any]],
    drive_id: str,
    canary: str,
    sp: SharePointRest,
    login: str,
    use_sp_view: bool = True,
) -> dict[str, dict[str, Any]]:
    """One read per arm and token (+ SharePoint's own can-read answer per arm)."""
    row: dict[str, dict[str, Any]] = {}
    for arm in ARMS:
        row[arm] = {}
        for name, token in tokens.items():
            cls, result = safe_read_once(token, drive_id, arms[arm]["file_id"], canary)
            row[arm][name] = entry(cls, result)
        if use_sp_view:
            row[arm]["sp"] = safe_sp_view(sp, arms[arm]["item"], login)
    return row


def phase1(
    gc: GraphClient,
    sp: SharePointRest,
    arms: dict[str, dict[str, Any]],
    user: dict[str, Any],
    drive_id: str,
    canary: str,
    opts: Opts,
    t_ref: float,
    report: dict[str, Any],
    saver: Saver,
) -> None:
    console.print(
        f"[bold]Phase 1: steady state, {opts.phase1:.0f}s, one token, nothing changes[/bold]"
    )
    token = sign_in(user["upn"], allow_device_code=False, use_cache=False)
    series: list[dict[str, Any]] = []
    report["phase1"] = {"series": series}
    start = time.monotonic()
    k = 0
    while True:
        time.sleep(max(0.0, start + k * opts.interval - time.monotonic()))
        row = read_arms({"tok": token}, arms, drive_id, canary, sp, arms["user_login"]["login"])
        t = round(time.monotonic() - t_ref, 1)
        series.append({"t": t, **row})
        if k % 6 == 0:
            shown = "  ".join(f"{a}={row[a]['tok']['class']}(sp {row[a]['sp']})" for a in ARMS)
            console.print(f"  t={t:6.1f}s  {shown}")
        if time.monotonic() - start >= opts.phase1:
            break
        k += 1
    report["phase1"]["summary"] = {
        arm: summarize(
            [r[arm]["tok"]["class"] for r in series],
            [r["t"] for r in series],
            [r[arm]["sp"] for r in series],
        )
        for arm in ARMS
    }
    saver.write(report)


def ensure_baseline(
    sp: SharePointRest,
    arms: dict[str, dict[str, Any]],
    user: dict[str, Any],
    drive_id: str,
    canary: str,
    opts: Opts,
    report: dict[str, Any],
    saver: Saver,
) -> dict[str, bool]:
    """Did each arm actually give access (RUN_LENGTH allowed reads in a row) before the removal?

    An arm that never had access has nothing to revoke: its instant "denial" in phase 2 would be
    misleading. Arms without a baseline after phase 1 get up to --baseline-wait seconds more; an arm
    still without access is flagged and left out of the stop condition and the conclusions.
    """
    series = report["phase1"]["series"]

    def has_run(arm: str) -> bool:
        last = [r[arm]["tok"]["class"] for r in series][-RUN_LENGTH:]
        return len(last) == RUN_LENGTH and all(c == "allowed" for c in last)

    ok = {arm: has_run(arm) for arm in ARMS}
    report["baseline"] = {"after_phase1": dict(ok), "extra_wait_s": 0.0}
    missing = [a for a in ARMS if not ok[a]]
    if not missing:
        return ok
    console.print(
        f"[yellow]No stable access yet for {missing}; "
        f"waiting up to {opts.baseline_wait:.0f}s[/yellow]"
    )
    token = sign_in(user["upn"], allow_device_code=False, use_cache=False)
    extra: list[dict[str, Any]] = []
    report["baseline"]["extra_series"] = extra
    runs = {a: 0 for a in missing}
    start = time.monotonic()
    k = 0
    while missing and time.monotonic() - start < opts.baseline_wait:
        time.sleep(max(0.0, start + k * opts.interval - time.monotonic()))
        row: dict[str, Any] = {"t": round(time.monotonic() - start, 1)}
        for arm in list(missing):
            cls, result = safe_read_once(token, drive_id, arms[arm]["file_id"], canary)
            row[arm] = entry(cls, result)
            runs[arm] = runs[arm] + 1 if cls == "allowed" else 0
            if runs[arm] >= RUN_LENGTH:
                ok[arm] = True
                missing.remove(arm)
        extra.append(row)
        k += 1
    report["baseline"]["extra_wait_s"] = round(time.monotonic() - start, 1)
    report["baseline"]["final"] = dict(ok)
    saver.write(report)
    if missing:
        console.print(f"[yellow]No baseline for {missing}: they never showed access[/yellow]")
    return ok


def phase2(
    gc: GraphClient,
    sp: SharePointRest,
    arms: dict[str, dict[str, Any]],
    user: dict[str, Any],
    drive_id: str,
    canary: str,
    opts: Opts,
    read_id: int,
    valid: dict[str, bool],
    report: dict[str, Any],
    saver: Saver,
) -> None:
    console.print("[bold]Phase 2: revocation in all arms back to back[/bold]")
    login = arms["user_login"]["login"]
    pre_token = sign_in(user["upn"], allow_device_code=False, use_cache=False)  # seconds before t0
    removers: dict[str, Callable[[], Any]] = {
        "entra": partial(gc.remove_group_member, arms["entra"]["group_id"], user["object_id"]),
        "spgroup": lambda: sp.remove_user_from_site_group(
            arms["spgroup"]["group_id"], arms["user_login"]["sp_id"]
        ),
        "direct": lambda: sp.remove_role_assignment(
            arms["direct"]["item"], arms["user_login"]["sp_id"], read_id
        ),
    }
    removed_at: dict[str, float] = {}
    removal_log: dict[str, Any] = {}
    t_first = time.monotonic()
    for arm, remove in removers.items():
        t_call = time.monotonic()
        try:
            remove()
        except (GraphError, SPRestError) as exc:
            body = exc.body if isinstance(exc, SPRestError) else str(exc)
            raise ProbeStop(
                f"Removal failed for arm {arm}.", {"arm": arm, "error": redact_payload(body)}
            ) from exc
        removed_at[arm] = time.monotonic()
        removal_log[arm] = {
            "call_seconds": round(removed_at[arm] - t_call, 2),
            "offset_from_first_call_s": round(t_call - t_first, 2),
        }
        console.print(f"  removed {arm} ({removal_log[arm]['call_seconds']}s)")
    fresh_started = time.monotonic()
    fresh_token = sign_in(user["upn"], allow_device_code=False, use_cache=False)
    tokens = {"pre": pre_token, "fresh": fresh_token}
    states = {arm: {name: new_state() for name in tokens} for arm in ARMS}
    series: list[dict[str, Any]] = []
    structure: list[dict[str, Any]] = []
    report["phase2"] = {
        "removals": removal_log,
        "fresh_token_fetch_seconds": round(time.monotonic() - fresh_started, 1),
        "series": series,
        "structure_checks": structure,
        "valid_arms": valid,
    }
    start = time.monotonic()
    last_struct = last_ckpt = last_print = -1e9
    k = 0
    while True:
        time.sleep(max(0.0, start + k * opts.interval - time.monotonic()))
        row = read_arms(tokens, arms, drive_id, canary, sp, login)
        now = time.monotonic()
        out: dict[str, Any] = {"t": round(now - start, 1)}
        for arm in ARMS:
            t_arm = round(now - removed_at[arm], 1)
            out[arm] = {"t_since_removal": t_arm, **row[arm]}
            for name in tokens:
                update_state(
                    states[arm][name], row[arm][name]["class"], t_arm, row[arm][name]["status"]
                )
        series.append(out)
        if now - last_struct >= STRUCTURE_CHECK_S or k == 0:
            last_struct = now
            snap = {"t": out["t"], **structure_check(gc, sp, user, arms)}
            structure.append(snap)
            console.print(f"  [dim]structure: {snap}[/dim]")
        if now - last_print >= 60 or k == 0:
            last_print = now
            shown = "  ".join(
                f"{a}: pre={row[a]['pre']['class'][:3]} fresh={row[a]['fresh']['class'][:3]}"
                for a in ARMS
            )
            console.print(f"  t={out['t']:6.1f}s  {shown}")
        if now - last_ckpt >= CHECKPOINT_S:
            last_ckpt = now
            report["phase2"]["summary"] = states
            saver.write(report)
        done = all(
            s["streak"] >= SETTLED_DENIALS
            for arm in ARMS
            if valid[arm]
            for s in states[arm].values()
        )
        if done:
            console.print("  every arm and token has 10 denials in a row: stopping early")
            break
        if now - start >= opts.phase2:
            console.print("[yellow]phase 2 time is up before every arm settled in denial[/yellow]")
            break
        k += 1
    report["phase2"]["summary"] = states
    saver.write(report)


def run_probe(
    rec: Recorder,
    gc: GraphClient,
    sp: SharePointRest,
    ctx: Ctx,
    opts: Opts,
    report: dict[str, Any],
    saver: Saver,
) -> None:
    settings = get_settings()
    location = settings.usage_location.strip().upper()
    if not ctx.password or len(location) != 2 or not location.isalpha():
        raise ProbeStop(
            "LEAKLAB_PERSONA_PASSWORD and a 2-letter LEAKLAB_USAGE_LOCATION are required."
        )
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
    arms = setup_arms(rec, gc, sp, ctx, opts, user, drive_id, library_url, canary, report)
    report["arms"] = {
        a: {k: v for k, v in arms[a].items() if k != "item"} for a in arms if a != "user_login"
    }
    saver.write(report)

    t_ref = time.monotonic()
    phase1(gc, sp, arms, user, drive_id, canary, opts, t_ref, report, saver)
    read_id = int(report["ids"]["read_role_definition_id"])
    valid = ensure_baseline(sp, arms, user, drive_id, canary, opts, report, saver)
    if not any(valid.values()):
        raise ProbeStop(
            "No arm ever showed stable access, so there is nothing to revoke.",
            {"baseline": report["baseline"].get("final", report["baseline"]["after_phase1"])},
        )
    phase2(gc, sp, arms, user, drive_id, canary, opts, read_id, valid, report, saver)


def run_cleanup(
    rec: Recorder,
    gc: GraphClient,
    sp: SharePointRest,
    ctx: Ctx,
    delete_users: bool,
    opts: Opts,
    report: dict[str, Any],
) -> None:
    p = ctx.prefix
    tag = f"-{opts.suffix}" if opts.suffix else ""
    _, drive_id, _ = resolve_site_and_drive(rec, gc, ctx.host, ctx.site_path, report)
    for arm in ARMS:
        delete_folder(rec, gc, drive_id, f"{p}sp-cons-{arm}{tag}", ctx, report)
    delete_groups(gc, [f"{p}probe-cons-ent{tag}"], ctx, report)
    spg_title = f"{p}probe-cons-spg{tag}"
    assert_scenario_name(spg_title, p)
    _, found = step(
        rec,
        "find SharePoint group",
        "GET /_api/web/sitegroups?$filter=Title eq '<title>'",
        lambda: sp.find_site_group(spg_title),
        lambda d: f"{len(d.get('value', []))} found",
    )
    for group in found.get("value", []):
        assert_scenario_name(str(group.get("Title")), p)
        step(
            rec,
            "delete SharePoint group",
            "POST /_api/web/sitegroups/removebyid(<id>)",
            partial(sp.delete_site_group, int(group["Id"])),
            lambda d: "deleted",
            "Delete the group by hand under Site permissions.",
        )
        report.setdefault("deleted", []).append({"type": "sharepoint group", "name": spg_title})
    if delete_users:
        targets = [(USER_ID, user_upn(ctx.spec, USER_ID))]
        if opts.throwaway_user:
            name = f"{p}probe-{opts.throwaway_user}"
            assert_scenario_name(name, p)
            targets = [(name, f"{name}@{ctx.domain}")]
        for label, upn in targets:
            found_user = gc.get_user_by_upn(upn)
            if found_user is not None:
                delete_or_gone("user", label, partial(gc.delete_user, found_user["id"]), report)


def print_results(report: dict[str, Any]) -> None:
    p1 = report.get("phase1", {}).get("summary")
    if p1:
        table = Table(title="Phase 1: steady state (nothing changes while polling)")
        for col in (
            "Arm",
            "Reads",
            "Allowed %",
            "Flip %",
            "First 5 allowed in a row (s)",
            "Longest allowed run",
            "SharePoint can-read %",
        ):
            table.add_column(col, overflow="fold")
        for arm, st in p1.items():
            table.add_row(
                arm,
                str(st["reads"]),
                str(st["allowed_pct"]),
                str(st["flip_pct"]),
                str(st["first_run_of_5_allowed_s"]),
                str(st["longest_allowed_run"]),
                str(st["sharepoint_can_read_pct"]),
            )
        console.print(table)
    p2 = report.get("phase2", {}).get("summary")
    if p2:
        table = Table(title="Phase 2: revocation (seconds after each arm's own removal)")
        for col in (
            "Arm",
            "Token",
            "Had access before t0?",
            "First deny (s)",
            "10 denies in a row from (s)",
            "Allowed reads after t0",
            "Reads (ok/deny/err)",
            "Came back?",
        ):
            table.add_column(col, overflow="fold")
        for arm, by_token in p2.items():
            for name, st in by_token.items():
                c = st["classes"]
                table.add_row(
                    arm,
                    name,
                    "yes"
                    if report["phase2"].get("valid_arms", {}).get(arm)
                    else "NO (nothing to revoke)",
                    str(st["first_denied_s"]),
                    str(st["settled_denied_s"]),
                    str(c["allowed"]),
                    f"{c['allowed']}/{c['denied']}/{c['error']}",
                    "YES" if st["flapped"] else "no",
                )
        console.print(table)
        console.print(f"removals: {report['phase2']['removals']}")


@app.command()
def main(
    cleanup: bool = typer.Option(False, "--cleanup", help="Delete the folders and both groups."),
    delete_users: bool = typer.Option(
        False, "--delete-users", help="With --cleanup: also the user."
    ),
    phase1_seconds: float = typer.Option(600.0, "--phase1", help="Steady-state polling seconds."),
    phase2_seconds: float = typer.Option(
        2700.0, "--phase2", help="Max revocation polling seconds."
    ),
    interval: float = typer.Option(5.0, help="Seconds between read cycles."),
    stable_timeout: float = typer.Option(180.0, help="Max seconds to wait for stable membership."),
    license_timeout: float = typer.Option(300.0, help="Max seconds to wait for the license."),
    wait: float = typer.Option(15.0, help="Seconds to wait after creating the Entra group/user."),
    sku_part_number: str = typer.Option("", help="License SKU part number (if not unique)."),
    throwaway_user: str = typer.Option("", help="Use a NEW user <prefix>probe-<NAME>."),
    suffix: str = typer.Option("", help="Append -<SUFFIX> to folder, file and group names."),
    baseline_wait: float = typer.Option(
        300.0, help="Extra seconds to wait for an arm that shows no access after phase 1."
    ),
    spec_path: Path = typer.Option(ROOT / "tenant_spec.yaml", help="Path to tenant_spec.yaml."),
) -> None:
    report: dict[str, Any] = {
        "probe": "consistency_probe",
        "run_at": datetime.now(UTC).isoformat(),
        "status": "running",
        "mode": "cleanup" if cleanup else "probe",
    }
    saver = Saver("cleanup" if cleanup else "run")
    report["output_file"] = saver.path.name
    console.print(f"Output file (new, never overwritten): {saver.path}")
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
            phase1=phase1_seconds,
            phase2=phase2_seconds,
            interval=interval,
            stable_timeout=stable_timeout,
            license_timeout=license_timeout,
            wait=wait,
            sku_part_number=sku_part_number,
            throwaway_user=throwaway_user,
            suffix=suffix,
            baseline_wait=baseline_wait,
        )
        with GraphClient() as gc, SharePointRest(site_url) as sp:
            if cleanup:
                run_cleanup(rec, gc, sp, ctx, delete_users, opts, report)
            else:
                run_probe(rec, gc, sp, ctx, opts, report, saver)
    except KeyboardInterrupt:
        report["status"] = "interrupted"
        console.print("[yellow]Interrupted: saving what was collected so far[/yellow]")
        print_results(report)
        saver.write(report)
        raise typer.Exit(code=130) from None
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
        saver.write(report)
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
        saver.write(report)
        raise typer.Exit(code=1) from exc

    report["status"] = "ok"
    print_results(report)
    saver.write(report)
    console.print(f"Saved redacted results to {saver.path}")


if __name__ == "__main__":
    app()
