"""Probe: does SharePoint really DENY/ALLOW reads for real users (not just permission listings)?

Question: with a folder whose ONLY grant is Read for group OUTER, can
  - p-plateng (member of INNER, nested INNER -> MID -> OUTER)  read the file?  (allowed only if
    SharePoint honors nested Entra groups)
  - p-intern  (in no probe group)                              read it?        (expected: denied)
  - the admin account (delegated, device code)                 read it?        (to be verified)
  - the driver app (app-only, CONTROL)                         read it?        (expected: allowed;
    proves the file exists and a denial is not just "missing file")

Steps (stops at the first hard failure, works around nothing):
  0. assert_test_tenant() on the Graph AND SharePoint app tokens' tid, before anything else.
  1. Ensure users p-intern and p-plateng (licensed, as in the spec) and wait until the license is
     visible in assignedLicenses (records how long it takes).
  2. Ensure groups <prefix>probe-outer/mid/inner, nest inner -> mid -> outer (retry 404), put
     p-plateng in inner. p-intern is in none.
  3. Create folder <prefix>access-folder with <prefix>access.txt (a canary). If the folder already
     exists the probe stops: run --cleanup first. Break inheritance on the folder
     (copyRoleAssignments=false, clearSubscopes=true), then ensureuser + addroleassignment of
     Read for OUTER only.
  4. Wait until transitiveMemberOf is STABLE (two consecutive consistent reads) for both users.
  5. Read the file content with DELEGATED tokens (Graph GET /drives/{d}/items/{id}/content,
     scope Files.Read.All) as each subject, --reads times (default 3), --interval seconds apart
     (default 10). Record HTTP status and whether the canary is in the body.
     Graph answers with a redirect to a pre-authenticated download URL; the probe follows it
     WITHOUT the Authorization header and never prints or saves that URL.
  6. Rich table + probes/out/access_probe.json (redacted: no tokens, no passwords, no URLs).

The admin signs in with DEVICE CODE: the probe prints a code and URL; complete the sign-in in a
browser when asked (just before the reads). The admin's own password is never used by the probe.

--cleanup deletes the access folder and the three probe groups. Persona users are deleted only
with --delete-users (other probes reuse them).

Run:  uv run python probes/access_probe.py [--admin-upn you@tenant.onmicrosoft.com]
"""

import hashlib
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import httpx
import typer
from rich.console import Console
from rich.markup import escape
from rich.pretty import Pretty
from rich.table import Table

from graph_probe import add_with_retry
from leaklab.auth import (
    GRAPH_DEFAULT_SCOPE,
    AuthError,
    PersonaSignInError,
    get_app_token,
    get_persona_token,
    sp_scope,
)
from leaklab.config import assert_test_tenant, get_settings, redact_jwt, redact_payload
from leaklab.graph_client import GRAPH_BASE_URL, GraphClient, GraphError
from leaklab.sp_rest import SharePointRest, SPRestError, folder_item_path
from leaklab.spec import TenantSpec, load_spec, user_mail_nickname, user_upn
from persona_probe import TARGET_IDS, ProbeStop, choose_sku, ensure_user
from sp_probe import Recorder, StepFailed, resolve_site_and_drive, step

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "probes" / "out"
OUT_FILES = {
    "probe": OUT_DIR / "access_probe.json",
    "cleanup": OUT_DIR / "access_probe_cleanup.json",
}
GROUP_SHORTS = ("outer", "mid", "inner")
READ_SCOPES = ["https://graph.microsoft.com/Files.Read.All"]
REDIRECT_STATUSES = (301, 302, 303, 307, 308)

console = Console()
app = typer.Typer(add_completion=False)


def save(report: dict[str, Any]) -> None:
    out_file = OUT_FILES[report["mode"]]
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    console.print(f"Saved redacted results to {out_file}")


def assert_access_name(name: str, prefix: str) -> None:
    """Safety: only objects named "<prefix>access-..." or "<prefix>probe-..." are ever touched."""
    if not name.startswith((f"{prefix}access-", f"{prefix}probe-")):
        raise RuntimeError(f"Refusing to touch {name!r}: not an access/probe object.")


def describe_assignments(data: dict[str, Any]) -> str:
    """One-line view of REST roleassignments: "<member> -> [role names]"."""
    return "; ".join(
        f"{a.get('Member', {}).get('Title')} -> "
        f"{[r.get('Name') for r in a.get('RoleDefinitionBindings', [])]}"
        for a in data.get("value", [])
    )


def read_file_as(token: str, drive_id: str, item_id: str, canary: str) -> dict[str, Any]:
    """Read a file's content with `token` through Graph. Records status and canary presence.

    The redirect target is a pre-authenticated URL: it is followed only if it is on
    *.sharepoint.com, without the Authorization header, and is never stored or printed.
    """
    url = f"{GRAPH_BASE_URL}/drives/{drive_id}/items/{item_id}/content"
    result: dict[str, Any] = {}
    with httpx.Client(timeout=30.0) as client:
        first = client.get(url, headers={"Authorization": f"Bearer {token}"})
        result["first_status"] = first.status_code
        body = first
        if first.status_code in REDIRECT_STATUSES:
            location = first.headers.get("Location", "")
            host = urlsplit(location).hostname or ""
            if not host.endswith(".sharepoint.com"):
                result["final_status"] = None
                result["note"] = f"redirect to unexpected host {host!r} not followed"
                result["canary_found"] = False
                return result
            body = client.get(location)  # pre-authenticated; no Authorization header
        result["final_status"] = body.status_code
        if body.is_success:
            result["canary_found"] = canary in body.text
        else:
            result["canary_found"] = False
            try:
                result["error_code"] = body.json().get("error", {}).get("code")
            except ValueError:
                result["error_code"] = "non_json_error"
    return result


def wait_membership_stable(
    gc: GraphClient,
    user_id: str,
    group_ids: set[str],
    expect_member: bool,
    timeout_s: float,
    interval_s: float,
) -> dict[str, Any]:
    """Poll transitiveMemberOf until two consecutive reads both match the expectation."""
    polls: list[dict[str, Any]] = []
    start = time.monotonic()
    consecutive = 0
    while True:
        seen = {g["id"] for g in gc.list_transitive_member_of(user_id)} & group_ids
        matches = seen == group_ids if expect_member else not seen
        consecutive = consecutive + 1 if matches else 0
        elapsed = time.monotonic() - start
        polls.append(
            {"elapsed_s": round(elapsed, 1), "probe_groups_seen": len(seen), "ok": matches}
        )
        console.print(
            f"  t={elapsed:5.1f}s  sees {len(seen)}/{len(group_ids)}  ok-in-a-row={consecutive}"
        )
        if consecutive >= 2:
            return {"stable": True, "after_s": round(elapsed, 1), "polls": polls}
        if elapsed + interval_s > timeout_s:
            return {"stable": False, "after_s": round(elapsed, 1), "polls": polls}
        time.sleep(interval_s)


def wait_license_visible(
    gc: GraphClient, user_id: str, sku_id: str, timeout_s: float, interval_s: float = 10.0
) -> dict[str, Any]:
    start = time.monotonic()
    while True:
        assigned = {
            lic.get("skuId") for lic in gc.get_user_licensing(user_id).get("assignedLicenses", [])
        }
        elapsed = time.monotonic() - start
        if sku_id in assigned:
            return {"visible": True, "after_s": round(elapsed, 1)}
        if elapsed + interval_s > timeout_s:
            return {"visible": False, "after_s": round(elapsed, 1)}
        time.sleep(interval_s)


def sign_in(upn: str, **kwargs: bool) -> str:
    try:
        return get_persona_token(upn, READ_SCOPES, **kwargs)
    except PersonaSignInError as exc:
        raise ProbeStop(
            "Sign-in failed; not worked around.",
            {
                "upn": upn,
                "error": exc.error,
                "error_codes": exc.error_codes,
                "description": exc.description,
                "interaction_required": exc.interaction_required,
            },
        ) from exc


def run_probe(
    rec: Recorder,
    gc: GraphClient,
    sp: SharePointRest,
    spec: TenantSpec,
    *,
    host: str,
    site_path: str,
    admin_upn: str,
    sku_part_number: str,
    reads: int,
    interval_s: float,
    stable_timeout_s: float,
    license_timeout_s: float,
    report: dict[str, Any],
) -> None:
    settings = get_settings()
    prefix = spec.naming.prefix
    password = settings.persona_password.get_secret_value()
    location = settings.usage_location.strip().upper()
    if not password or len(location) != 2 or not location.isalpha():
        raise ProbeStop(
            "LEAKLAB_PERSONA_PASSWORD and a 2-letter LEAKLAB_USAGE_LOCATION are required."
        )
    folder_name, file_name = f"{prefix}access-folder", f"{prefix}access.txt"
    assert_access_name(folder_name, prefix)
    canary = "CANARY-ACC-" + hashlib.sha256(f"{spec.naming.seed}|access".encode()).hexdigest()[:6]
    report["canary"] = canary

    console.print("[bold]1. Site, users and licenses[/bold]")
    _, drive_id, library_url = resolve_site_and_drive(rec, gc, host, site_path, report)
    sku_id = choose_sku(gc.list_subscribed_skus(), sku_part_number, report)
    users: dict[str, dict[str, Any]] = {}
    for user_id in TARGET_IDS:
        users[user_id] = ensure_user(gc, spec, user_id, password, location, sku_id, report)
        users[user_id].pop("created_at_monotonic", None)
    for user_id, record in users.items():
        record["license_wait"] = wait_license_visible(
            gc, record["object_id"], sku_id, license_timeout_s
        )
        console.print(f"  {user_id}: license visible={record['license_wait']}")
    report["users"] = list(users.values())

    console.print("[bold]2. Groups and nesting[/bold]")
    report["memberships"] = []
    groups: dict[str, dict[str, Any]] = {}
    for short in GROUP_SHORTS:
        name = f"{prefix}probe-{short}"
        assert_access_name(name, prefix)
        found = gc.find_group_by_name(name)
        state = "reused"
        if found is None:
            found = gc.create_security_group(name, name)
            state = "created"
        groups[short] = found
        report.setdefault("groups", {})[short] = {"id": found["id"], "name": name, "state": state}
        console.print(f"  group {name}: {state}")
    add_with_retry(
        lambda: gc.add_group_to_group(groups["mid"]["id"], groups["inner"]["id"]),
        "inner in mid",
        report,
    )
    add_with_retry(
        lambda: gc.add_group_to_group(groups["outer"]["id"], groups["mid"]["id"]),
        "mid in outer",
        report,
    )
    add_with_retry(
        lambda: gc.add_user_to_group(groups["inner"]["id"], users["p-plateng"]["object_id"]),
        "p-plateng in inner",
        report,
    )

    console.print("[bold]3. Folder, file, unique permissions, grant to OUTER only[/bold]")
    status, _ = step(
        rec,
        "find access folder",
        "GET /drives/{drive}/root:/<folder>",
        lambda: gc.request("GET", f"/drives/{drive_id}/root:/{quote(folder_name)}"),
        lambda d: "exists",
        allow=(404,),
    )
    if status != 404:
        raise ProbeStop(f"{folder_name!r} already exists; run --cleanup first for a clean result.")
    _, folder = step(
        rec,
        "create access folder",
        "POST /drives/{drive}/root/children",
        lambda: gc.request(
            "POST",
            f"/drives/{drive_id}/root/children",
            json={"name": folder_name, "folder": {}, "@microsoft.graph.conflictBehavior": "fail"},
        ),
        lambda d: f"created, id={d.get('id')}",
    )
    _, file = step(
        rec,
        "upload access file",
        "PUT /drives/{drive}/items/{folder-id}:/<file>:/content",
        lambda: gc.request(
            "PUT",
            f"/drives/{drive_id}/items/{folder['id']}:/{quote(file_name)}:/content",
            content=f"LeakLab access_probe test document.\nReference code: {canary}\n",
            headers={"Content-Type": "text/plain"},
        ),
        lambda d: f"id={d.get('id')} size={d.get('size')}",
    )
    report["ids"] = {
        "drive_id": drive_id,
        "folder_id": folder["id"],
        "file_id": file["id"],
        "outer_group_id": groups["outer"]["id"],
    }
    folder_item = folder_item_path(f"{library_url}/{folder_name}")
    step(
        rec,
        "break inheritance (folder)",
        "POST <folder item>/breakroleinheritance(copyRoleAssignments=false,clearSubscopes=true)",
        lambda: sp.break_role_inheritance(
            folder_item, copy_role_assignments=False, clear_subscopes=True
        ),
        lambda d: "ok",
        "Next probe: re-read HasUniqueRoleAssignments on the folder.",
    )
    claim = f"c:0t.c|tenant|{groups['outer']['id']}"
    _, principal = step(
        rec,
        "ensureuser (OUTER group claim)",
        "POST /_api/web/ensureuser",
        lambda: sp.ensure_user(claim),
        lambda d: f"Id={d.get('Id')} PrincipalType={d.get('PrincipalType')}",
        "Group may not be replicated to SharePoint yet; rerun after a minute.",
    )
    _, roles = step(
        rec,
        "role definitions",
        "GET /_api/web/roledefinitions",
        lambda: sp.get_role_definitions(),
        lambda d: f"{len(d.get('value', []))} roles",
    )
    read_defs = [r for r in roles.get("value", []) if r.get("Name") == "Read"]
    if len(read_defs) != 1:
        raise ProbeStop("Expected exactly one role named 'Read'.", {"roles": roles})
    step(
        rec,
        "grant Read to OUTER on folder",
        "POST <folder item>/roleassignments/addroleassignment",
        lambda: sp.add_role_assignment(folder_item, int(principal["Id"]), int(read_defs[0]["Id"])),
        lambda d: "ok",
    )
    t_grant = time.monotonic()
    step(
        rec,
        "folder role assignments (check)",
        "GET <folder item>/roleassignments?$expand=Member,RoleDefinitionBindings",
        lambda: sp.get_role_assignments(folder_item),
        describe_assignments,
    )

    console.print("[bold]4. Wait for stable group membership[/bold]")
    group_ids = {g["id"] for g in groups.values()}
    stable: dict[str, Any] = {}
    for user_id, expect in (("p-plateng", True), ("p-intern", False)):
        console.print(f" {user_id} (expect {'all 3 groups' if expect else 'none'})")
        stable[user_id] = wait_membership_stable(
            gc, users[user_id]["object_id"], group_ids, expect, stable_timeout_s, 5.0
        )
        if not stable[user_id]["stable"]:
            report["membership_stability"] = stable
            raise ProbeStop(f"Membership of {user_id} not stable within {stable_timeout_s:.0f}s.")
    report["membership_stability"] = stable

    console.print("[bold]5. Tokens[/bold]")
    subjects: list[dict[str, Any]] = []
    for user_id, expected in (
        ("p-plateng", "allowed if SharePoint honors nested groups"),
        ("p-intern", "denied"),
    ):
        token = sign_in(users[user_id]["upn"], allow_device_code=False)
        subjects.append({"subject": user_id, "token": token, "expected": expected})
    subjects.append(
        {
            "subject": "driver app (app-only, control)",
            "token": get_app_token(GRAPH_DEFAULT_SCOPE),
            "expected": "allowed (file exists)",
        }
    )
    console.print(f"[bold]Admin sign-in needed now[/bold]: sign in as {admin_upn} when prompted.")
    admin_token = sign_in(admin_upn, use_password=False)
    subjects.insert(2, {"subject": "admin", "token": admin_token, "expected": "to be verified"})
    report["subjects"] = [{"subject": s["subject"], "expected": s["expected"]} for s in subjects]

    console.print(f"[bold]6. Reads: {reads} rounds, {interval_s:.0f}s apart[/bold]")
    results: list[dict[str, Any]] = []
    report["reads"] = results
    for round_no in range(1, reads + 1):
        for s in subjects:
            outcome = read_file_as(s["token"], drive_id, str(file["id"]), canary)
            outcome.update(
                subject=s["subject"],
                round=round_no,
                seconds_since_grant=round(time.monotonic() - t_grant, 1),
            )
            results.append(outcome)
            console.print(
                f"  round {round_no} {s['subject']}: first={outcome['first_status']} "
                f"final={outcome.get('final_status')} canary={outcome['canary_found']}"
            )
        if round_no < reads:
            time.sleep(interval_s)

    summary: list[dict[str, Any]] = []
    for s in subjects:
        mine = [r for r in results if r["subject"] == s["subject"]]
        outcomes = [(r.get("final_status"), r["canary_found"]) for r in mine]
        summary.append(
            {
                "subject": s["subject"],
                "expected": s["expected"],
                "outcomes": [{"final_status": o[0], "canary_found": o[1]} for o in outcomes],
                "flapping": len(set(outcomes)) > 1,
            }
        )
    report["summary"] = summary


def run_cleanup(
    rec: Recorder,
    gc: GraphClient,
    spec: TenantSpec,
    *,
    host: str,
    site_path: str,
    delete_users: bool,
    report: dict[str, Any],
) -> None:
    prefix = spec.naming.prefix
    folder_name = f"{prefix}access-folder"
    assert_access_name(folder_name, prefix)
    report["deleted"] = []
    _, drive_id, _ = resolve_site_and_drive(rec, gc, host, site_path, report)
    status, folder = step(
        rec,
        "find access folder",
        "GET /drives/{drive}/root:/<folder>",
        lambda: gc.request("GET", f"/drives/{drive_id}/root:/{quote(folder_name)}"),
        lambda d: "exists",
        allow=(404,),
    )
    if status != 404:
        assert_access_name(str(folder.get("name")), prefix)
        step(
            rec,
            "delete access folder",
            "DELETE /drives/{drive}/items/{folder-id}",
            lambda: gc.request("DELETE", f"/drives/{drive_id}/items/{folder['id']}"),
            lambda d: "deleted (recycle bin)",
        )
        report["deleted"].append({"type": "folder", "name": folder_name})
    for short in GROUP_SHORTS:
        name = f"{prefix}probe-{short}"
        assert_access_name(name, prefix)
        group = gc.find_group_by_name(name)
        if group is None:
            console.print(f"group {name}: not found")
            continue
        gc.delete_group(group["id"])
        report["deleted"].append({"type": "group", "name": name})
        console.print(f"group {name}: deleted")
    if delete_users:
        for user_id in TARGET_IDS:
            assert user_mail_nickname(spec, user_id).startswith(prefix)
            found = gc.get_user_by_upn(user_upn(spec, user_id))
            if found is None:
                continue
            gc.delete_user(found["id"])
            report["deleted"].append({"type": "user", "upn": found["userPrincipalName"]})
            console.print(f"user {found['userPrincipalName']}: deleted")


def print_results(report: dict[str, Any]) -> None:
    table = Table(title="access_probe: file reads as each subject")
    for col in ("Subject", "Expected", "Round 1", "Round 2", "Round 3", "Flapping"):
        table.add_column(col, overflow="fold")
    for s in report.get("summary", []):
        cells = [
            f"{o['final_status']} / canary={'yes' if o['canary_found'] else 'no'}"
            for o in s["outcomes"]
        ]
        cells += [""] * (3 - len(cells))
        table.add_row(s["subject"], s["expected"], *cells[:3], "YES" if s["flapping"] else "no")
    console.print(table)


@app.command()
def main(
    cleanup: bool = typer.Option(
        False, "--cleanup", help="Delete the access folder + probe groups."
    ),
    delete_users: bool = typer.Option(
        False, "--delete-users", help="With --cleanup: also persona users."
    ),
    admin_upn: str = typer.Option("", help="Admin UPN (default: LEAKLAB_ADMIN_UPN)."),
    sku_part_number: str = typer.Option("", help="License SKU part number (if not unique)."),
    reads: int = typer.Option(3, help="Reads per subject."),
    interval: float = typer.Option(10.0, help="Seconds between read rounds."),
    stable_timeout: float = typer.Option(180.0, help="Max seconds to wait for stable membership."),
    license_timeout: float = typer.Option(
        300.0, help="Max seconds to wait for license visibility."
    ),
    spec_path: Path = typer.Option(ROOT / "tenant_spec.yaml", help="Path to tenant_spec.yaml."),
) -> None:
    report: dict[str, Any] = {
        "probe": "access_probe",
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
        admin = (admin_upn or settings.admin_upn).strip()
        if not cleanup and not admin:
            raise ValueError("Set LEAKLAB_ADMIN_UPN in .env or pass --admin-upn.")
        report.update(prefix=spec.naming.prefix, admin_upn=admin or None)

        for scope in (GRAPH_DEFAULT_SCOPE, sp_scope()):  # safety guard first
            assert_test_tenant(str(redact_jwt(get_app_token(scope)).get("tid", "")))
        console.print("[green]Tenant guard passed (Graph and SharePoint tokens)[/green]")

        with GraphClient() as gc, SharePointRest(site_url) as sp:
            if cleanup:
                run_cleanup(
                    rec,
                    gc,
                    spec,
                    host=host,
                    site_path=parts.path.rstrip("/"),
                    delete_users=delete_users,
                    report=report,
                )
            else:
                run_probe(
                    rec,
                    gc,
                    sp,
                    spec,
                    host=host,
                    site_path=parts.path.rstrip("/"),
                    admin_upn=admin,
                    sku_part_number=sku_part_number,
                    reads=reads,
                    interval_s=interval,
                    stable_timeout_s=stable_timeout,
                    license_timeout_s=license_timeout,
                    report=report,
                )
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
                "HTTP status": exc.status,
                "body": redact_payload(exc.body),
                "next probe": exc.hint,
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
