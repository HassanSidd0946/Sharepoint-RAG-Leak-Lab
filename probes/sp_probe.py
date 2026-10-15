"""Probe: how do we break inheritance, grant a group, and read permissions back?

Goal: discover, with real responses, the permission operations the generator will need.
Nothing here assumes a result; every response is recorded (redacted) in
probes/out/sp_probe.json and the summary table shows what happened.

Steps (stops at the first failure, prints the redacted error body and a suggested next probe):
  0. assert_test_tenant() on the Graph AND SharePoint app tokens' tid, before anything else.
  1. Graph: resolve the LeakLab site and its default drive. Plain SharePoint REST GET /web as a
     reachability check for the app-only SharePoint token.
  2. Graph: create folder "<prefix>probe-folder" (reused if it exists) and upload
     "<prefix>probe.txt" (containing a test canary) into it.
  3. BEFORE any change: Graph GET .../items/{id}/permissions for the file and the folder, plus
     the SharePoint REST view (HasUniqueRoleAssignments + roleassignments) of both.
  4. REST: break inheritance on the folder (copyRoleAssignments=false, clearSubscopes=true).
  5. Resolve group "<prefix>probe-inner" (created via Graph if missing) as a SharePoint
     principal with web/ensureuser using its Entra claim; look up the "Read" role definition
     id via REST; add a Read role assignment on the folder.
  6. AFTER: read permissions again via Graph and via REST (Member + RoleDefinitionBindings).
  7. REST: read the site's Owners / Members / Visitors groups and their members.
  8. Print a summary table.

--cleanup: deletes only the probe folder (and the file inside it) and exits. The probe group
is left alone; remove it with `graph_probe.py --cleanup`.

Run:  uv run python probes/sp_probe.py            then   uv run python probes/sp_probe.py --cleanup
"""

import hashlib
import json
import time
from collections.abc import Callable
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlsplit

import httpx
import typer
from rich.console import Console
from rich.pretty import Pretty
from rich.table import Table

from leaklab.auth import GRAPH_DEFAULT_SCOPE, AuthError, get_app_token, sp_scope
from leaklab.config import assert_test_tenant, get_settings, redact_jwt, redact_payload
from leaklab.graph_client import GraphClient, GraphError
from leaklab.sp_rest import (
    GroupKind,
    SharePointRest,
    SPRestError,
    file_item_path,
    folder_item_path,
    parse_body,
)
from leaklab.spec import load_spec

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "probes" / "out"
# Separate files so a --cleanup run can never overwrite the probe results.
OUT_FILES = {"probe": OUT_DIR / "sp_probe.json", "cleanup": OUT_DIR / "sp_probe_cleanup.json"}

GROUP_KINDS: tuple[GroupKind, ...] = ("owner", "member", "visitor")

console = Console()
app = typer.Typer(add_completion=False)


class StepFailed(Exception):
    """A probe step failed; carries everything needed to report it."""

    def __init__(self, operation: str, label: str, status: int, body: Any, hint: str) -> None:
        super().__init__(f"{operation} failed (HTTP {status})")
        self.operation = operation
        self.label = label
        self.status = status
        self.body = body
        self.hint = hint


class Recorder:
    """Collects every step (for the JSON file and the summary table)."""

    def __init__(self, report: dict[str, Any]) -> None:
        self.report = report
        self.rows: list[tuple[str, str, str, str]] = []
        report["steps"] = []

    def add(self, operation: str, label: str, status: int, summary: str, response: Any) -> None:
        self.report["steps"].append(
            {
                "operation": operation,
                "endpoint": label,
                "status": status,
                "summary": summary,
                "response": redact_payload(response),
            }
        )
        self.rows.append((operation, label, str(status), summary))


def step(
    rec: Recorder,
    operation: str,
    label: str,
    call: Callable[[], httpx.Response],
    summarize: Callable[[Any], str] = lambda _d: "",
    hint: str = "",
    allow: tuple[int, ...] = (),
) -> tuple[int, Any]:
    """Run one HTTP call, record it, and return (status, parsed body).

    HTTP errors listed in `allow` (e.g. 404 on a lookup) are returned, not raised. Any other
    error raises StepFailed.
    """
    try:
        resp = call()
    except GraphError as exc:
        status: int = exc.status_code
        body: Any = {"code": exc.code, "message": exc.message}
    except SPRestError as exc:
        status, body = exc.status_code, exc.body
    else:
        data = parse_body(resp)
        rec.add(operation, label, resp.status_code, summarize(data), data)
        return resp.status_code, data
    if status in allow:
        rec.add(operation, label, status, "not found (allowed)", body)
        return status, body
    rec.add(operation, label, status, "FAILED", body)
    raise StepFailed(operation, label, status, body, hint)


def require(condition: bool, operation: str, message: str, hint: str, data: Any = None) -> None:
    if not condition:
        raise StepFailed(operation, message, 0, redact_payload(data), hint)


def save(report: dict[str, Any]) -> None:
    out_file = OUT_FILES[report["mode"]]
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    console.print(f"Saved redacted results to {out_file}")


def print_table(rows: list[tuple[str, str, str, str]]) -> None:
    table = Table(title="sp_probe summary", show_lines=False)
    for col in ("Operation", "Endpoint", "HTTP", "Result"):
        table.add_column(col, overflow="fold")
    for row in rows:
        table.add_row(*row)
    console.print(table)


def assert_probe_name(name: str, prefix: str) -> None:
    """Safety: we only ever touch objects named "<prefix>probe-...". Anything else aborts."""
    if not name.startswith(f"{prefix}probe-"):
        raise RuntimeError(f"Refusing to touch {name!r}: not a probe object.")


def resolve_site_and_drive(
    rec: Recorder, gc: GraphClient, host: str, site_path: str, report: dict[str, Any]
) -> tuple[str, str, str]:
    """Return (site_id, drive_id, library server-relative URL, e.g. /sites/X/Shared Documents)."""
    hint = "Check LEAKLAB_SITE_URL and that the site exists; next probe: GET /sites?search=LeakLab."
    _, site = step(
        rec,
        "resolve site",
        f"GET /sites/{host}:{site_path}",
        lambda: gc.request("GET", f"/sites/{host}:{site_path}"),
        lambda d: f"id={d.get('id')}",
        hint,
    )
    site_id = str(site["id"])
    _, drive = step(
        rec,
        "resolve drive",
        "GET /sites/{site-id}/drive",
        lambda: gc.request("GET", f"/sites/{site_id}/drive"),
        lambda d: f"id={d.get('id')} name={d.get('name')}",
        "Next probe: GET /sites/{site-id}/drives to list libraries.",
    )
    drive_id = str(drive["id"])
    library_url = unquote(urlsplit(str(drive["webUrl"])).path)
    report["ids"] = {
        "site_id": site_id,
        "drive_id": drive_id,
        "drive_name": drive.get("name"),
        "library_server_relative_url": library_url,
    }
    return site_id, drive_id, library_url


def graph_permissions(
    rec: Recorder, gc: GraphClient, drive_id: str, phase: str, items: dict[str, str]
) -> None:
    for what, item_id in items.items():
        step(
            rec,
            f"Graph permissions of {what} ({phase})",
            f"GET /drives/{{drive}}/items/{{{what}-id}}/permissions",
            partial(gc.request, "GET", f"/drives/{drive_id}/items/{item_id}/permissions"),
            lambda d: f"{len(d.get('value', []))} permission entries",
            "Next probe: GET the same item with ?$expand=permissions, or check Files.* roles.",
        )


def rest_permissions(rec: Recorder, sp: SharePointRest, phase: str, items: dict[str, str]) -> None:
    for what, item_path in items.items():
        step(
            rec,
            f"REST unique flag of {what} ({phase})",
            f"GET {what} ListItemAllFields?$select=HasUniqueRoleAssignments",
            partial(sp.get_unique_flag, item_path),
            lambda d: f"HasUniqueRoleAssignments={d.get('HasUniqueRoleAssignments')}",
            "Next probe: GET the item's ListItemAllFields without $select and read its properties.",
        )
        step(
            rec,
            f"REST roleassignments of {what} ({phase})",
            f"GET {what} roleassignments?$expand=Member,RoleDefinitionBindings",
            partial(sp.get_role_assignments, item_path),
            lambda d: (
                "; ".join(
                    f"{a.get('Member', {}).get('Title')} -> "
                    f"{[r.get('Name') for r in a.get('RoleDefinitionBindings', [])]}"
                    for a in d.get("value", [])
                )
                or "no assignments"
            ),
            "Next probe: GET .../roleassignments without $expand, then add one $expand at a time.",
        )


def run_probe(
    rec: Recorder,
    gc: GraphClient,
    sp: SharePointRest,
    *,
    prefix: str,
    seed: int,
    host: str,
    site_path: str,
    group_wait: float,
    report: dict[str, Any],
) -> None:
    folder_name = f"{prefix}probe-folder"
    file_name = f"{prefix}probe.txt"
    group_name = f"{prefix}probe-inner"
    for name in (folder_name, group_name):  # the file lives inside the folder; same prefix
        assert_probe_name(name, prefix)
    canary = "CANARY-PRB-" + hashlib.sha256(f"{seed}|sp-probe".encode()).hexdigest()[:6]
    report["canary"] = canary

    console.print("[bold]1. Site, drive, SharePoint REST reachability[/bold]")
    _, drive_id, library_url = resolve_site_and_drive(rec, gc, host, site_path, report)
    step(
        rec,
        "REST reachability",
        "GET /_api/web?$select=Title,Url",
        lambda: sp.request("GET", "/web", params={"$select": "Title,Url"}),
        lambda d: f"Title={d.get('Title')}",
        "The app-only SharePoint token was rejected or the site URL is wrong. Next probe: decode "
        "the WWW-Authenticate header of a bare GET /_api/web, and re-check the token audience.",
    )

    console.print("[bold]2. Folder and file[/bold]")
    status, folder = step(
        rec,
        "find probe folder",
        "GET /drives/{drive}/root:/<folder>",
        lambda: gc.request("GET", f"/drives/{drive_id}/root:/{quote(folder_name)}"),
        lambda d: f"exists, id={d.get('id')}",
        allow=(404,),
    )
    if status == 404:
        _, folder = step(
            rec,
            "create probe folder",
            "POST /drives/{drive}/root/children",
            lambda: gc.request(
                "POST",
                f"/drives/{drive_id}/root/children",
                json={
                    "name": folder_name,
                    "folder": {},
                    "@microsoft.graph.conflictBehavior": "fail",
                },
            ),
            lambda d: f"created, id={d.get('id')}",
            "Next probe: create the folder by hand in the SharePoint UI and re-run.",
        )
    folder_id = str(folder["id"])
    _, file = step(
        rec,
        "upload probe file",
        "PUT /drives/{drive}/items/{folder-id}:/<file>:/content",
        lambda: gc.request(
            "PUT",
            f"/drives/{drive_id}/items/{folder_id}:/{quote(file_name)}:/content",
            content=f"LeakLab sp_probe test document.\nReference code: {canary}\n",
            headers={"Content-Type": "text/plain"},
        ),
        lambda d: f"id={d.get('id')} size={d.get('size')}",
        "Next probe: upload the file by hand in the SharePoint UI and re-run.",
    )
    file_id = str(file["id"])
    report["ids"].update(folder_id=folder_id, file_id=file_id)
    folder_url = f"{library_url}/{folder_name}"
    file_url = f"{folder_url}/{file_name}"
    folder_item, file_item = folder_item_path(folder_url), file_item_path(file_url)
    report["ids"].update(folder_server_relative_url=folder_url, file_server_relative_url=file_url)

    console.print("[bold]3. Permissions BEFORE any change[/bold]")
    graph_permissions(rec, gc, drive_id, "before", {"file": file_id, "folder": folder_id})
    rest_permissions(rec, sp, "before", {"file": file_item, "folder": folder_item})
    for entry in report["steps"]:
        if "REST unique flag of folder (before)" in entry["operation"]:
            if entry["response"] and entry["response"].get("HasUniqueRoleAssignments"):
                console.print(
                    "[yellow]Folder already had unique permissions (previous run?). "
                    "Run --cleanup first for a clean baseline.[/yellow]"
                )

    console.print("[bold]4. Break inheritance on the folder (REST)[/bold]")
    step(
        rec,
        "break inheritance (folder)",
        "POST <folder item>/breakroleinheritance(copyRoleAssignments=false,clearSubscopes=true)",
        lambda: sp.break_role_inheritance(
            folder_item, copy_role_assignments=False, clear_subscopes=True
        ),
        lambda d: "ok" if d is None else f"body={d}",
        "App-only REST may need a form digest. Next probe: POST /_api/contextinfo with the same "
        "token, then retry with X-RequestDigest.",
    )

    console.print("[bold]5. Group principal and Read role assignment[/bold]")
    _, found = step(
        rec,
        "find probe group",
        "GET /groups?$filter=displayName eq '<group>'",
        lambda: httpx_wrap(lambda: gc.find_group_by_name(group_name)),
        lambda d: "exists" if d else "missing",
    )
    if not found:
        _, found = step(
            rec,
            "create probe group",
            "POST /groups",
            lambda: httpx_wrap(lambda: gc.create_security_group(group_name, group_name), 201),
            lambda d: f"created, id={d.get('id')}",
            "Run graph_probe.py first, or create the group by hand.",
        )
        console.print(f"Waiting {group_wait:.0f}s for the new group to replicate...")
        time.sleep(group_wait)
    group_id = str(found["id"])
    report["ids"]["group_id"] = group_id
    # Verified 2026-10-15: this claim format works for an Entra security group. Only this one
    # format is tried; if it fails the probe stops instead of guessing another.
    claim = f"c:0t.c|tenant|{group_id}"
    report["group_claim_tried"] = claim
    _, principal = step(
        rec,
        "ensureuser (group claim)",
        "POST /_api/web/ensureuser",
        lambda: sp.ensure_user(claim),
        lambda d: (
            f"Id={d.get('Id')} LoginName={d.get('LoginName')} "
            f"PrincipalType={d.get('PrincipalType')}"
        ),
        "The claim format c:0t.c|tenant|<group-id> was not accepted. Next probe: add the group to "
        "the site by hand (Site permissions > Share), then GET /_api/web/siteusers and read its "
        "real LoginName. Do not guess other formats.",
    )
    require(
        isinstance(principal, dict) and "Id" in principal,
        "ensureuser (group claim)",
        "response has no Id",
        "Inspect the saved ensureuser response and probe /_api/web/siteusers.",
        principal,
    )
    _, roles = step(
        rec,
        "role definitions",
        "GET /_api/web/roledefinitions",
        lambda: sp.get_role_definitions(),
        lambda d: ", ".join(f"{r.get('Name')}={r.get('Id')}" for r in d.get("value", [])),
        "Next probe: GET /_api/web/roledefinitions without $select.",
    )
    read_defs = [r for r in roles.get("value", []) if r.get("Name") == "Read"]
    require(
        len(read_defs) == 1,
        "role definitions",
        f"expected exactly one role named 'Read', found {len(read_defs)}",
        "Inspect the saved role definitions (names may be localized); check RoleTypeKind.",
        roles,
    )
    read_id = int(read_defs[0]["Id"])
    report["ids"]["read_role_definition_id"] = read_id
    report["ids"]["read_role_type_kind"] = read_defs[0].get("RoleTypeKind")
    step(
        rec,
        "add Read role assignment (folder)",
        "POST <folder item>/roleassignments/addroleassignment(principalid,roledefid)",
        lambda: sp.add_role_assignment(folder_item, int(principal["Id"]), read_id),
        lambda d: "ok" if d is None else f"body={d}",
        "Next probe: read the folder's current roleassignments and retry on a throwaway folder.",
    )

    console.print("[bold]6. Permissions AFTER[/bold]")
    graph_permissions(rec, gc, drive_id, "after", {"file": file_id, "folder": folder_id})
    rest_permissions(rec, sp, "after", {"file": file_item, "folder": folder_item})

    console.print("[bold]7. Site groups[/bold]")
    for kind in GROUP_KINDS:
        _, grp = step(
            rec,
            f"associated {kind} group",
            f"GET /_api/web/associated{kind}group",
            partial(sp.get_associated_group, kind),
            lambda d: f"Id={d.get('Id')} Title={d.get('Title')}",
            "Next probe: GET /_api/web/sitegroups to list all groups.",
        )
        step(
            rec,
            f"{kind} group members",
            f"GET /_api/web/sitegroups/getbyid({grp.get('Id')})/users",
            partial(sp.get_group_users, int(grp["Id"])),
            lambda d: (
                "; ".join(
                    f"{u.get('Title')} [{u.get('LoginName')}] type={u.get('PrincipalType')}"
                    for u in d.get("value", [])
                )
                or "no members"
            ),
            "Next probe: GET /_api/web/siteusers.",
        )


def httpx_wrap(call: Callable[[], Any], status: int = 200) -> httpx.Response:
    """Adapt a GraphClient helper (returns a dict) to the step() interface (needs a Response)."""
    return httpx.Response(status, json=call())


def run_cleanup(
    rec: Recorder,
    gc: GraphClient,
    *,
    prefix: str,
    host: str,
    site_path: str,
    report: dict[str, Any],
) -> None:
    folder_name = f"{prefix}probe-folder"
    assert_probe_name(folder_name, prefix)
    _, drive_id, _ = resolve_site_and_drive(rec, gc, host, site_path, report)
    status, folder = step(
        rec,
        "find probe folder",
        "GET /drives/{drive}/root:/<folder>",
        lambda: gc.request("GET", f"/drives/{drive_id}/root:/{quote(folder_name)}"),
        lambda d: f"exists, id={d.get('id')}",
        allow=(404,),
    )
    if status == 404:
        console.print("Probe folder not found; nothing to delete.")
        return
    assert_probe_name(str(folder.get("name")), prefix)
    step(
        rec,
        "delete probe folder",
        "DELETE /drives/{drive}/items/{folder-id}",
        lambda: gc.request("DELETE", f"/drives/{drive_id}/items/{folder['id']}"),
        lambda _d: "deleted (goes to the site recycle bin)",
        "Delete the folder by hand in the SharePoint UI.",
    )


@app.command()
def main(
    cleanup: bool = typer.Option(False, "--cleanup", help="Only delete the probe folder."),
    spec_path: Path = typer.Option(ROOT / "tenant_spec.yaml", help="Path to tenant_spec.yaml."),
    group_wait: float = typer.Option(20.0, help="Seconds to wait after creating the probe group."),
) -> None:
    report: dict[str, Any] = {
        "probe": "sp_probe",
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
        site_parts = urlsplit(site_url)
        if not site_url or site_parts.hostname != host:
            raise ValueError("LEAKLAB_SITE_URL must be set and use the LEAKLAB_SP_HOST host.")
        site_path = site_parts.path.rstrip("/")
        report.update(prefix=spec.naming.prefix, site_url=site_url)

        # Safety guard first: both tokens we are about to use must be from the test tenant.
        for scope in (GRAPH_DEFAULT_SCOPE, sp_scope()):
            assert_test_tenant(str(redact_jwt(get_app_token(scope)).get("tid", "")))
        console.print("[green]Tenant guard passed (Graph and SharePoint tokens)[/green]")

        with GraphClient() as gc, SharePointRest(site_url) as sp:
            if cleanup:
                run_cleanup(
                    rec,
                    gc,
                    prefix=spec.naming.prefix,
                    host=host,
                    site_path=site_path,
                    report=report,
                )
            else:
                run_probe(
                    rec,
                    gc,
                    sp,
                    prefix=spec.naming.prefix,
                    seed=spec.naming.seed,
                    host=host,
                    site_path=site_path,
                    group_wait=group_wait,
                    report=report,
                )
    except StepFailed as exc:
        report["status"] = "failed"
        report["failed_step"] = {
            "operation": exc.operation,
            "endpoint": exc.label,
            "http_status": exc.status,
            "body": redact_payload(exc.body),
            "suggested_next_probe": exc.hint,
        }
        print_table(rec.rows)
        console.print(f"[bold red]FAILED at:[/bold red] {exc.operation} ({exc.label})")
        console.print(f"HTTP status: {exc.status}")
        console.print("Error body (redacted):")
        console.print(Pretty(redact_payload(exc.body)))
        console.print(f"[bold]Suggested next probe:[/bold] {exc.hint}")
        save(report)
        raise typer.Exit(code=1) from exc
    except (AuthError, RuntimeError, ValueError, OSError, httpx.HTTPError) as exc:
        report["status"] = "failed"
        report["error"] = f"{type(exc).__name__}: {exc}"
        console.print(f"[bold red]FAILED:[/bold red] {report['error']}")
        save(report)
        raise typer.Exit(code=1) from exc

    report["status"] = "ok"
    print_table(rec.rows)
    save(report)


if __name__ == "__main__":
    app()
