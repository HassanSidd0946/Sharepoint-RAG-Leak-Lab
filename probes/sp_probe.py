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

SUBCOMMANDS (each saves its own probes/out/sp_probe_<name>.json; add --cleanup to undo):
  baseline          the original probe described above (was the default command)
  root-grant        Read for an Entra group at the SITE ROOT; does an inheriting folder show it?
  nested-folder     unique child folder under a unique parent, a different group on each
  file-grant        break inheritance on one FILE, grant a USER; records the real user claim
  move              move that unique file into a folder with different unique permissions
  idempotency       the same role assignment / group-member add twice: exact statuses

Run:  uv run python probes/sp_probe.py <subcommand> [--cleanup]
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
from urllib.parse import quote, unquote, urlsplit

import httpx
import typer
from rich.console import Console
from rich.pretty import Pretty
from rich.table import Table

from leaklab.auth import GRAPH_DEFAULT_SCOPE, AuthError, get_app_token, sp_scope
from leaklab.config import assert_test_tenant, get_settings, redact_jwt, redact_payload
from leaklab.graph_client import GRAPH_BASE_URL, GraphClient, GraphError
from leaklab.sp_rest import (
    GroupKind,
    SharePointRest,
    SPRestError,
    file_item_path,
    folder_item_path,
    parse_body,
)
from leaklab.spec import TenantSpec, load_spec

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "probes" / "out"


def out_file_for(mode: str) -> Path:
    """Output file of a run. Cleanup runs get their own file so they never overwrite results.

    The original probe keeps its names (sp_probe.json / sp_probe_cleanup.json); subcommands use
    sp_probe_<mode>.json, e.g. sp_probe_root-grant.json and sp_probe_root-grant-cleanup.json.
    """
    legacy = {"probe": "sp_probe.json", "cleanup": "sp_probe_cleanup.json"}
    return OUT_DIR / legacy.get(mode, f"sp_probe_{mode}.json")


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
        rec.add(operation, label, status, f"HTTP {status} (allowed)", body)
        return status, body
    rec.add(operation, label, status, "FAILED", body)
    raise StepFailed(operation, label, status, body, hint)


def require(condition: bool, operation: str, message: str, hint: str, data: Any = None) -> None:
    if not condition:
        raise StepFailed(operation, message, 0, redact_payload(data), hint)


def save(report: dict[str, Any]) -> None:
    out_file = out_file_for(report["mode"])
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


@app.command("baseline")
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


# ==========================================================================================
# Scenario subcommands. Each one saves its own probes/out/sp_probe_<name>.json (and
# sp_probe_<name>-cleanup.json for --cleanup). They record what SharePoint actually does and
# print the facts; they do not decide what the oracle should assume.
# ==========================================================================================

ALL_ERRORS: tuple[int, ...] = tuple(range(400, 600))
ADD_RETRIES = 6
ADD_RETRY_WAIT_S = 5.0


@dataclass
class Ctx:
    """Everything a scenario needs from settings and the spec."""

    spec: TenantSpec
    prefix: str
    seed: int
    host: str
    site_path: str
    site_url: str
    domain: str
    password: str


def assert_scenario_name(name: str, prefix: str) -> None:
    """Safety: only "<prefix>probe-..." groups/users and "<prefix>sp-..." folders are touched."""
    if not name.startswith((f"{prefix}probe-", f"{prefix}sp-")):
        raise RuntimeError(f"Refusing to touch {name!r}: not a scenario object.")


def describe(snap: dict[str, Any]) -> str:
    items = "; ".join(f"{a['member']} -> {a['roles']}" for a in snap["assignments"])
    return f"unique={snap['unique']}; {items or 'no assignments'}"


def simplify_assignments(data: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for a in data.get("value", []):
        member = a.get("Member", {})
        out.append(
            {
                "member": member.get("Title"),
                "login": member.get("LoginName"),
                "principal_type": member.get("PrincipalType"),
                "roles": sorted(str(r.get("Name")) for r in a.get("RoleDefinitionBindings", [])),
            }
        )
    return sorted(out, key=lambda x: str(x["login"]))


def ensure_group(
    gc: GraphClient, name: str, ctx: Ctx, wait_s: float, report: dict[str, Any]
) -> dict[str, Any]:
    assert_scenario_name(name, ctx.prefix)
    found = gc.find_group_by_name(name)
    state = "reused"
    if found is None:
        found = gc.create_security_group(name, name)
        state = "created"
        console.print(f"  group {name}: created; waiting {wait_s:.0f}s to replicate")
        time.sleep(wait_s)
    else:
        console.print(f"  group {name}: reused")
    report.setdefault("groups", {})[name] = {"id": found["id"], "state": state}
    return found


def ensure_probe_user(
    gc: GraphClient, ctx: Ctx, short: str, wait_s: float, report: dict[str, Any]
) -> dict[str, Any]:
    name = f"{ctx.prefix}probe-{short}"
    assert_scenario_name(name, ctx.prefix)
    if not ctx.password:
        raise StepFailed("probe user", "LEAKLAB_PERSONA_PASSWORD", 0, None, "Set it in .env.")
    upn = f"{name}@{ctx.domain}"
    found = gc.get_user_by_upn(upn)
    state = "reused"
    if found is None:
        found = gc.create_user(upn, name, name, ctx.password)
        state = "created"
        console.print(f"  user {upn}: created; waiting {wait_s:.0f}s to replicate")
        time.sleep(wait_s)
    else:
        console.print(f"  user {upn}: reused")
    report.setdefault("users", {})[name] = {"id": found["id"], "upn": upn, "state": state}
    return {**found, "upn": upn}


def create_folder(
    rec: Recorder,
    gc: GraphClient,
    drive_id: str,
    name: str,
    ctx: Ctx,
    parent_id: str | None = None,
) -> dict[str, Any]:
    assert_scenario_name(name, ctx.prefix)
    if parent_id:
        lookup = f"/drives/{drive_id}/items/{parent_id}:/{quote(name)}"
        create = f"/drives/{drive_id}/items/{parent_id}/children"
    else:
        lookup = f"/drives/{drive_id}/root:/{quote(name)}"
        create = f"/drives/{drive_id}/root/children"
    status, _ = step(
        rec,
        f"find folder {name}",
        "GET <folder lookup>",
        lambda: gc.request("GET", lookup),
        lambda d: "exists",
        allow=(404,),
    )
    require(
        status == 404,
        f"find folder {name}",
        f"{name!r} already exists",
        "Run this subcommand with --cleanup first for a clean result.",
    )
    _, folder = step(
        rec,
        f"create folder {name}",
        "POST <parent>/children",
        lambda: gc.request(
            "POST",
            create,
            json={"name": name, "folder": {}, "@microsoft.graph.conflictBehavior": "fail"},
        ),
        lambda d: f"id={d.get('id')}",
    )
    return dict(folder)


def upload_file(
    rec: Recorder, gc: GraphClient, drive_id: str, folder_id: str, name: str, text: str
) -> dict[str, Any]:
    _, file = step(
        rec,
        f"upload {name}",
        "PUT <folder>:/<file>:/content",
        lambda: gc.request(
            "PUT",
            f"/drives/{drive_id}/items/{folder_id}:/{quote(name)}:/content",
            content=text,
            headers={"Content-Type": "text/plain"},
        ),
        lambda d: f"id={d.get('id')}",
    )
    return dict(file)


def read_role_id(rec: Recorder, sp: SharePointRest, report: dict[str, Any]) -> int:
    _, roles = step(
        rec,
        "role definitions",
        "GET /_api/web/roledefinitions",
        lambda: sp.get_role_definitions(),
        lambda d: f"{len(d.get('value', []))} roles",
    )
    defs = [r for r in roles.get("value", []) if r.get("Name") == "Read"]
    require(len(defs) == 1, "role definitions", "expected exactly one 'Read' role", "", roles)
    role_id = int(defs[0]["Id"])
    report.setdefault("ids", {})["read_role_definition_id"] = role_id
    return role_id


def ensure_principal(
    rec: Recorder,
    sp: SharePointRest,
    logon_name: str,
    label: str,
    hint: str,
    wait_s: float = 120.0,
    interval_s: float = 5.0,
) -> dict[str, Any]:
    """POST /web/ensureuser. A brand-new Entra group can answer HTTP 400 "could not be found"
    for a while (seen 2026-10-15); that one error is retried every `interval_s` up to `wait_s`
    and the number of attempts and seconds are recorded (report["ensureuser_waits"]).
    Any other error stops the probe.
    """
    start = time.monotonic()
    attempts = 0
    while True:
        attempts += 1
        status, principal = step(
            rec,
            f"ensureuser ({label})",
            "POST /_api/web/ensureuser",
            lambda: sp.ensure_user(logon_name),
            lambda d: (
                f"Id={d.get('Id')} LoginName={d.get('LoginName')} type={d.get('PrincipalType')}"
            ),
            hint,
            allow=ALL_ERRORS,
        )
        elapsed = time.monotonic() - start
        if status < 400:
            break
        not_found = status == 400 and "could not be found" in json.dumps(principal).lower()
        if not not_found or elapsed + interval_s > wait_s:
            raise StepFailed(
                f"ensureuser ({label})", "POST /_api/web/ensureuser", status, principal, hint
            )
        console.print(f"  ensureuser ({label}): not found yet, retrying in {interval_s:.0f}s")
        time.sleep(interval_s)
    rec.report.setdefault("ensureuser_waits", {})[label] = {
        "attempts": attempts,
        "seconds": round(elapsed, 1),
    }
    require(
        isinstance(principal, dict) and "Id" in principal,
        f"ensureuser ({label})",
        "response has no Id",
        hint,
        principal,
    )
    return dict(principal)


def group_principal(rec: Recorder, sp: SharePointRest, group_id: str, label: str) -> dict[str, Any]:
    return ensure_principal(
        rec,
        sp,
        f"c:0t.c|tenant|{group_id}",
        label,
        "Group may not be replicated to SharePoint yet; rerun with a larger --wait.",
    )


def grant_read(
    rec: Recorder,
    sp: SharePointRest,
    item_path: str,
    principal_id: int,
    role_id: int,
    label: str,
    allow: tuple[int, ...] = (),
) -> tuple[int, Any]:
    return step(
        rec,
        label,
        "POST <item>/roleassignments/addroleassignment(principalid,roledefid)",
        lambda: sp.add_role_assignment(item_path, principal_id, role_id),
        lambda d: "ok",
        "Next probe: read the item's roleassignments and retry on a throwaway item.",
        allow=allow,
    )


def break_inheritance(rec: Recorder, sp: SharePointRest, item_path: str, label: str) -> None:
    step(
        rec,
        label,
        "POST <item>/breakroleinheritance(copyRoleAssignments=false,clearSubscopes=true)",
        lambda: sp.break_role_inheritance(
            item_path, copy_role_assignments=False, clear_subscopes=True
        ),
        lambda d: "ok",
        "Next probe: re-read HasUniqueRoleAssignments on the item.",
    )


def snapshot(
    rec: Recorder,
    sp: SharePointRest,
    label: str,
    item_path: str,
    report: dict[str, Any],
    retry_404: int = 0,
) -> dict[str, Any]:
    """REST view of an item: HasUniqueRoleAssignments + role assignments (stored in the report)."""
    flag: Any = None
    for attempt in range(retry_404 + 1):
        last = attempt == retry_404
        status, flag = step(
            rec,
            f"REST unique flag: {label}",
            "GET <item>?$select=HasUniqueRoleAssignments",
            lambda: sp.get_unique_flag(item_path),
            lambda d: f"HasUniqueRoleAssignments={d.get('HasUniqueRoleAssignments')}",
            allow=() if last else (404,),
        )
        if status != 404:
            break
        console.print(f"  {label}: HTTP 404, retrying in 5s")
        time.sleep(5)
    _, assigns = step(
        rec,
        f"REST roleassignments: {label}",
        "GET <item>/roleassignments?$expand=Member,RoleDefinitionBindings",
        lambda: sp.get_role_assignments(item_path),
        lambda d: "; ".join(f"{a['member']} -> {a['roles']}" for a in simplify_assignments(d)),
    )
    snap = {
        "unique": bool(flag["HasUniqueRoleAssignments"]),
        "assignments": simplify_assignments(assigns),
    }
    report.setdefault("snapshots", {})[label] = snap
    console.print(f"  {label}: {describe(snap)}")
    return snap


def graph_snapshot(
    rec: Recorder,
    gc: GraphClient,
    drive_id: str,
    item_id: str,
    label: str,
    report: dict[str, Any],
) -> list[dict[str, Any]]:
    _, data = step(
        rec,
        f"Graph permissions: {label}",
        "GET /drives/{drive}/items/{id}/permissions",
        lambda: gc.request("GET", f"/drives/{drive_id}/items/{item_id}/permissions"),
        lambda d: f"{len(d.get('value', []))} entries",
    )
    simplified = []
    for perm in data.get("value", []):
        v2 = perm.get("grantedToV2", {})
        who = next(
            (
                v2[k].get("displayName")
                for k in ("group", "user", "siteGroup", "siteUser")
                if k in v2
            ),
            None,
        )
        login = v2.get("siteUser", {}).get("loginName") or v2.get("siteGroup", {}).get("loginName")
        simplified.append({"who": who, "login": login, "roles": sorted(perm.get("roles", []))})
    report.setdefault("graph_snapshots", {})[label] = simplified
    console.print(f"  Graph {label}: {[(s['who'], s['roles']) for s in simplified]}")
    return simplified


def has_member(snap: dict[str, Any], login: str, role: str = "Read") -> bool:
    return any(a["login"] == login and role in a["roles"] for a in snap["assignments"])


def delete_folder(
    rec: Recorder, gc: GraphClient, drive_id: str, name: str, ctx: Ctx, report: dict[str, Any]
) -> None:
    assert_scenario_name(name, ctx.prefix)
    status, folder = step(
        rec,
        f"find folder {name}",
        "GET /drives/{drive}/root:/<folder>",
        lambda: gc.request("GET", f"/drives/{drive_id}/root:/{quote(name)}"),
        lambda d: "exists",
        allow=(404,),
    )
    if status == 404:
        console.print(f"folder {name}: not found")
        return
    assert_scenario_name(str(folder.get("name")), ctx.prefix)
    step(
        rec,
        f"delete folder {name}",
        "DELETE /drives/{drive}/items/{id}",
        lambda: gc.request("DELETE", f"/drives/{drive_id}/items/{folder['id']}"),
        lambda d: "deleted (recycle bin)",
    )
    report.setdefault("deleted", []).append({"type": "folder", "name": name})


def delete_or_gone(
    kind: str, name: str, delete: Callable[[], None], report: dict[str, Any]
) -> None:
    """Run a Graph delete. A 404 means it is already gone: deletes lag behind GETs (seen
    2026-10-15: GET /users/{upn} still found a user whose DELETE then returned 404)."""
    try:
        delete()
    except GraphError as exc:
        if exc.status_code != 404:
            raise
        result = "already gone (404 on delete)"
    else:
        result = "deleted"
    report.setdefault("deleted", []).append({"type": kind, "name": name, "result": result})
    console.print(f"{kind} {name}: {result}")


def delete_groups(gc: GraphClient, names: list[str], ctx: Ctx, report: dict[str, Any]) -> None:
    for name in names:
        assert_scenario_name(name, ctx.prefix)
        group = gc.find_group_by_name(name)
        if group is None:
            console.print(f"group {name}: not found")
            continue
        delete_or_gone("group", name, partial(gc.delete_group, group["id"]), report)


def delete_user(gc: GraphClient, ctx: Ctx, short: str, report: dict[str, Any]) -> None:
    name = f"{ctx.prefix}probe-{short}"
    assert_scenario_name(name, ctx.prefix)
    found = gc.get_user_by_upn(f"{name}@{ctx.domain}")
    if found is None:
        console.print(f"user {name}: not found")
        return
    delete_or_gone("user", name, lambda: gc.delete_user(found["id"]), report)


Work = Callable[[Recorder, GraphClient, SharePointRest, Ctx, dict[str, Any]], None]


def run_command(mode: str, cleanup: bool, work: Work, cleanup_work: Work, spec_path: Path) -> None:
    """Shared runner: guard first, clients, error handling, output file for this subcommand."""
    report: dict[str, Any] = {
        "probe": f"sp_probe {mode}",
        "run_at": datetime.now(UTC).isoformat(),
        "status": "running",
        "mode": f"{mode}-cleanup" if cleanup else mode,
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

        with GraphClient() as gc, SharePointRest(site_url) as sp:
            (cleanup_work if cleanup else work)(rec, gc, sp, ctx, report)
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
        console.print(f"[bold red]FAILED:[/bold red] {report['error']}")
        save(report)
        raise typer.Exit(code=1) from exc

    report["status"] = "ok"
    print_table(rec.rows)
    save(report)


CLEANUP_OPT = typer.Option(False, "--cleanup", help="Delete only this subcommand's objects.")
WAIT_OPT = typer.Option(15.0, help="Seconds to wait after creating groups/users (replication).")
SPEC_OPT = typer.Option(ROOT / "tenant_spec.yaml", help="Path to tenant_spec.yaml.")


# ---- a) root-grant ------------------------------------------------------------------------


def work_root_grant(
    rec: Recorder,
    gc: GraphClient,
    sp: SharePointRest,
    ctx: Ctx,
    report: dict[str, Any],
    *,
    wait: float,
) -> None:
    p = ctx.prefix
    console.print("[bold]root-grant: Read for an Entra group at the SITE ROOT[/bold]")
    _, drive_id, library_url = resolve_site_and_drive(rec, gc, ctx.host, ctx.site_path, report)
    group = ensure_group(gc, f"{p}probe-rootgrant", ctx, wait, report)
    principal = group_principal(rec, sp, group["id"], "root-grant group")
    read_id = read_role_id(rec, sp, report)
    login = str(principal["LoginName"])

    web_before = snapshot(rec, sp, "site root (before)", "/web", report)
    folder_name = f"{p}sp-root-folder"
    folder = create_folder(rec, gc, drive_id, folder_name, ctx)
    folder_item = folder_item_path(f"{library_url}/{folder_name}")
    snapshot(rec, sp, "inheriting folder (before)", folder_item, report)

    grant_read(rec, sp, "/web", int(principal["Id"]), read_id, "grant Read at SITE ROOT")
    web_after = snapshot(rec, sp, "site root (after)", "/web", report)
    folder_after = snapshot(rec, sp, "inheriting folder (after)", folder_item, report)
    graph_after = graph_snapshot(
        rec, gc, drive_id, str(folder["id"]), "inheriting folder (after)", report
    )

    others_before = [(a["login"], a["roles"]) for a in web_before["assignments"]]
    others_after = [
        (a["login"], a["roles"]) for a in web_after["assignments"] if a["login"] != login
    ]
    report["evaluation"] = {
        "web_lists_group_with_read": has_member(web_after, login),
        "folder_still_inherits": not folder_after["unique"],
        "folder_lists_group_with_read": has_member(folder_after, login),
        "graph_folder_lists_group": any(s["login"] == login for s in graph_after),
        "other_root_assignments_unchanged": others_before == others_after,
    }
    console.print(Pretty(report["evaluation"]))


def cleanup_root_grant(
    rec: Recorder, gc: GraphClient, sp: SharePointRest, ctx: Ctx, report: dict[str, Any]
) -> None:
    p = ctx.prefix
    _, drive_id, _ = resolve_site_and_drive(rec, gc, ctx.host, ctx.site_path, report)
    group_name = f"{p}probe-rootgrant"
    assert_scenario_name(group_name, p)
    group = gc.find_group_by_name(group_name)
    if group is not None:
        claim = f"c:0t.c|tenant|{group['id']}"
        # Remove the site-root assignment BEFORE the group is deleted (else it is orphaned).
        _, raw = step(
            rec,
            "site root roleassignments (find ours)",
            "GET /_api/web/roleassignments?$expand=Member,RoleDefinitionBindings",
            lambda: sp.get_role_assignments("/web"),
            lambda d: f"{len(d.get('value', []))} assignments",
        )
        ours = [a for a in raw.get("value", []) if a.get("Member", {}).get("LoginName") == claim]
        if ours:
            read_id = read_role_id(rec, sp, report)
            step(
                rec,
                "remove Read assignment at SITE ROOT",
                "POST /web/roleassignments/removeroleassignment(principalid,roledefid)",
                lambda: sp.remove_role_assignment("/web", int(ours[0]["PrincipalId"]), read_id),
                lambda d: "ok",
                "Remove the group's assignment by hand (Site permissions), then delete the group.",
            )
            _, again = step(
                rec,
                "site root roleassignments (verify removal)",
                "GET /_api/web/roleassignments?$expand=Member",
                lambda: sp.get_role_assignments("/web"),
                lambda d: f"{len(d.get('value', []))} assignments",
            )
            still = [
                a for a in again.get("value", []) if a.get("Member", {}).get("LoginName") == claim
            ]
            require(
                not still,
                "remove Read assignment at SITE ROOT",
                "assignment still present after removal",
                "Remove it by hand in Site permissions; the group was NOT deleted.",
                again,
            )
        else:
            console.print("No site-root assignment for the probe group found.")
    delete_folder(rec, gc, drive_id, f"{p}sp-root-folder", ctx, report)
    delete_groups(gc, [group_name], ctx, report)


@app.command("root-grant")
def cmd_root_grant(
    cleanup: bool = CLEANUP_OPT, wait: float = WAIT_OPT, spec_path: Path = SPEC_OPT
) -> None:
    """(a) Grant Read to an Entra group at the site root; check an inheriting folder shows it."""
    run_command(
        "root-grant", cleanup, partial(work_root_grant, wait=wait), cleanup_root_grant, spec_path
    )


# ---- b) nested-folder ---------------------------------------------------------------------


def work_nested_folder(
    rec: Recorder,
    gc: GraphClient,
    sp: SharePointRest,
    ctx: Ctx,
    report: dict[str, Any],
    *,
    wait: float,
) -> None:
    p = ctx.prefix
    console.print(
        "[bold]nested-folder: unique parent, then a unique child with another group[/bold]"
    )
    _, drive_id, library_url = resolve_site_and_drive(rec, gc, ctx.host, ctx.site_path, report)
    group_a = ensure_group(gc, f"{p}probe-nest-a", ctx, 0.0, report)
    group_b = ensure_group(gc, f"{p}probe-nest-b", ctx, wait, report)
    pa = group_principal(rec, sp, group_a["id"], "group A")
    pb = group_principal(rec, sp, group_b["id"], "group B")
    read_id = read_role_id(rec, sp, report)
    login_a, login_b = str(pa["LoginName"]), str(pb["LoginName"])

    parent_name, child_name = f"{p}sp-nest-parent", f"{p}sp-nest-child"
    parent = create_folder(rec, gc, drive_id, parent_name, ctx)
    parent_url = f"{library_url}/{parent_name}"
    parent_item = folder_item_path(parent_url)
    break_inheritance(rec, sp, parent_item, "break inheritance (PARENT)")
    grant_read(rec, sp, parent_item, int(pa["Id"]), read_id, "grant Read to A on PARENT")

    child = create_folder(rec, gc, drive_id, child_name, ctx, parent_id=str(parent["id"]))
    child_url = f"{parent_url}/{child_name}"
    child_item = folder_item_path(child_url)
    upload_file(rec, gc, drive_id, str(child["id"]), f"{p}sp-nest.txt", "LeakLab nested test\n")
    file_item = file_item_path(f"{child_url}/{p}sp-nest.txt")

    child_inherited = snapshot(rec, sp, "child (new, before its own break)", child_item, report)
    break_inheritance(rec, sp, child_item, "break inheritance (CHILD)")
    grant_read(rec, sp, child_item, int(pb["Id"]), read_id, "grant Read to B on CHILD")

    parent_after = snapshot(rec, sp, "parent (after)", parent_item, report)
    child_after = snapshot(rec, sp, "child (after)", child_item, report)
    file_after = snapshot(rec, sp, "file in child (after)", file_item, report)
    graph_snapshot(rec, gc, drive_id, str(child["id"]), "child (after)", report)

    report["evaluation"] = {
        "new_child_inherited_parent_unique_grant_A": (
            not child_inherited["unique"] and has_member(child_inherited, login_a)
        ),
        "parent_unique_and_only_A": (
            parent_after["unique"]
            and [a["login"] for a in parent_after["assignments"]] == [login_a]
        ),
        "child_unique_and_only_B": (
            child_after["unique"] and [a["login"] for a in child_after["assignments"]] == [login_b]
        ),
        "file_inherits_child_and_shows_only_B": (
            not file_after["unique"]
            and [a["login"] for a in file_after["assignments"]] == [login_b]
        ),
    }
    console.print(Pretty(report["evaluation"]))


def cleanup_nested_folder(
    rec: Recorder, gc: GraphClient, sp: SharePointRest, ctx: Ctx, report: dict[str, Any]
) -> None:
    p = ctx.prefix
    _, drive_id, _ = resolve_site_and_drive(rec, gc, ctx.host, ctx.site_path, report)
    delete_folder(rec, gc, drive_id, f"{p}sp-nest-parent", ctx, report)  # includes the child
    delete_groups(gc, [f"{p}probe-nest-a", f"{p}probe-nest-b"], ctx, report)


@app.command("nested-folder")
def cmd_nested_folder(
    cleanup: bool = CLEANUP_OPT, wait: float = WAIT_OPT, spec_path: Path = SPEC_OPT
) -> None:
    """(b) Subfolder under a unique-permission folder; break it and grant a different group."""
    run_command(
        "nested-folder",
        cleanup,
        partial(work_nested_folder, wait=wait),
        cleanup_nested_folder,
        spec_path,
    )


# ---- c) file-grant ------------------------------------------------------------------------


def user_logon(upn: str, style: str) -> str:
    """Logon name to send to ensureuser: the plain UPN, or the assumed membership claim."""
    return upn if style == "upn" else f"i:0#.f|membership|{upn}"


def work_file_grant(
    rec: Recorder,
    gc: GraphClient,
    sp: SharePointRest,
    ctx: Ctx,
    report: dict[str, Any],
    *,
    wait: float,
    style: str,
) -> None:
    p = ctx.prefix
    console.print("[bold]file-grant: unique permissions on a single FILE, granted to a USER[/bold]")
    _, drive_id, library_url = resolve_site_and_drive(rec, gc, ctx.host, ctx.site_path, report)
    user = ensure_probe_user(gc, ctx, "a", wait, report)
    folder_name, file_name = f"{p}sp-grant-folder", f"{p}sp-grant.txt"
    folder = create_folder(rec, gc, drive_id, folder_name, ctx)
    file = upload_file(rec, gc, drive_id, str(folder["id"]), file_name, "LeakLab file-grant test\n")
    folder_url = f"{library_url}/{folder_name}"
    folder_item = folder_item_path(folder_url)
    file_item = file_item_path(f"{folder_url}/{file_name}")

    snapshot(rec, sp, "file (before, inherits)", file_item, report)
    break_inheritance(rec, sp, file_item, "break inheritance (FILE)")
    logon = user_logon(user["upn"], style)
    principal = ensure_principal(
        rec,
        sp,
        logon,
        "user",
        f"The logon name {logon!r} was not accepted. Next probe: rerun with --logon-name "
        f"{'claim' if style == 'upn' else 'upn'} (an explicit retry, not a guess).",
    )
    read_id = read_role_id(rec, sp, report)
    grant_read(rec, sp, file_item, int(principal["Id"]), read_id, "grant Read to USER on FILE")

    file_after = snapshot(rec, sp, "file (after)", file_item, report)
    folder_after = snapshot(rec, sp, "folder (after, untouched)", folder_item, report)
    graph_snapshot(rec, gc, drive_id, str(file["id"]), "file (after)", report)

    returned = str(principal["LoginName"])
    assumed = f"i:0#.f|membership|{user['upn'].lower()}"
    report["claim_check"] = {
        "logon_name_sent": logon,
        "style": style,
        "login_name_returned": returned,
        "equals_assumed_claim": returned.lower() == assumed,
        "principal_type": principal.get("PrincipalType"),
        "title": principal.get("Title"),
        "email": principal.get("Email"),
        "user_principal_name": principal.get("UserPrincipalName"),
    }
    report["evaluation"] = {
        "file_now_unique": file_after["unique"],
        "file_lists_user_with_read": has_member(file_after, returned),
        "file_lists_only_the_user": [a["login"] for a in file_after["assignments"]] == [returned],
        "folder_unchanged_and_inheriting": not folder_after["unique"],
    }
    console.print(Pretty(report["claim_check"]))
    console.print(Pretty(report["evaluation"]))


def cleanup_file_grant(
    rec: Recorder, gc: GraphClient, sp: SharePointRest, ctx: Ctx, report: dict[str, Any]
) -> None:
    _, drive_id, _ = resolve_site_and_drive(rec, gc, ctx.host, ctx.site_path, report)
    delete_folder(rec, gc, drive_id, f"{ctx.prefix}sp-grant-folder", ctx, report)
    delete_user(gc, ctx, "a", report)


@app.command("file-grant")
def cmd_file_grant(
    cleanup: bool = CLEANUP_OPT,
    wait: float = WAIT_OPT,
    logon_name: str = typer.Option(
        "upn",
        help="What to send to ensureuser: 'upn' (plain UPN) or 'claim' (i:0#.f|membership|upn).",
    ),
    spec_path: Path = SPEC_OPT,
) -> None:
    """(c) Break inheritance on one file and grant a user; record the real user claim."""
    if logon_name not in ("upn", "claim"):
        raise typer.BadParameter("must be 'upn' or 'claim'")
    run_command(
        "file-grant",
        cleanup,
        partial(work_file_grant, wait=wait, style=logon_name),
        cleanup_file_grant,
        spec_path,
    )


# ---- d) move ------------------------------------------------------------------------------


def work_move(
    rec: Recorder,
    gc: GraphClient,
    sp: SharePointRest,
    ctx: Ctx,
    report: dict[str, Any],
    *,
    wait: float,
    style: str,
) -> None:
    p = ctx.prefix
    console.print(
        "[bold]move: a uniquely-permissioned file moves into another unique folder[/bold]"
    )
    _, drive_id, library_url = resolve_site_and_drive(rec, gc, ctx.host, ctx.site_path, report)
    user = ensure_probe_user(gc, ctx, "a", 0.0, report)
    group = ensure_group(gc, f"{p}probe-move-dst", ctx, wait, report)
    pg = group_principal(rec, sp, group["id"], "destination group")
    read_id = read_role_id(rec, sp, report)

    src_name, dst_name, file_name = f"{p}sp-move-src", f"{p}sp-move-dst", f"{p}sp-move.txt"
    src = create_folder(rec, gc, drive_id, src_name, ctx)
    dst = create_folder(rec, gc, drive_id, dst_name, ctx)
    file = upload_file(rec, gc, drive_id, str(src["id"]), file_name, "LeakLab move test\n")
    src_url, dst_url = f"{library_url}/{src_name}", f"{library_url}/{dst_name}"
    dst_item = folder_item_path(dst_url)
    old_file_item = file_item_path(f"{src_url}/{file_name}")

    # File: unique, granted to the user.
    break_inheritance(rec, sp, old_file_item, "break inheritance (FILE, in source)")
    logon = user_logon(user["upn"], style)
    pu = ensure_principal(
        rec, sp, logon, "user", "Rerun with --logon-name claim (explicit retry) if rejected."
    )
    grant_read(rec, sp, old_file_item, int(pu["Id"]), read_id, "grant Read to USER on FILE")
    # Destination folder: unique, granted to a DIFFERENT principal (the group).
    break_inheritance(rec, sp, dst_item, "break inheritance (DESTINATION folder)")
    grant_read(rec, sp, dst_item, int(pg["Id"]), read_id, "grant Read to GROUP on DESTINATION")

    console.print("[bold]BEFORE the move[/bold]")
    before = snapshot(rec, sp, "file (before move)", old_file_item, report)
    dst_snap = snapshot(rec, sp, "destination folder", dst_item, report)
    graph_before = graph_snapshot(rec, gc, drive_id, str(file["id"]), "file (before move)", report)

    console.print("[bold]Move (Graph PATCH parentReference)[/bold]")
    _, moved = step(
        rec,
        "move file",
        "PATCH /drives/{drive}/items/{file-id}  {parentReference: {id: <destination>}}",
        lambda: gc.request(
            "PATCH",
            f"/drives/{drive_id}/items/{file['id']}",
            json={"parentReference": {"id": dst["id"]}},
        ),
        lambda d: f"id={d.get('id')} parent={d.get('parentReference', {}).get('name')}",
        "Next probe: move the file by hand in the SharePoint UI and read its permissions.",
    )
    report.setdefault("ids", {})["file_id_before"] = file["id"]
    report["ids"]["file_id_after"] = moved.get("id")
    new_file_item = file_item_path(f"{dst_url}/{file_name}")

    console.print("[bold]AFTER the move[/bold]")
    after = snapshot(rec, sp, "file (after move)", new_file_item, report, retry_404=3)
    graph_after = graph_snapshot(
        rec, gc, drive_id, str(moved.get("id")), "file (after move)", report
    )

    report["evaluation"] = {
        "item_id_unchanged": moved.get("id") == file["id"],
        "file_kept_own_unique_permissions": (
            after["unique"] and after["assignments"] == before["assignments"]
        ),
        "file_now_inherits_destination": (
            not after["unique"] and after["assignments"] == dst_snap["assignments"]
        ),
        "before_unique": before["unique"],
        "after_unique": after["unique"],
        "graph_principals_before": [s["login"] for s in graph_before],
        "graph_principals_after": [s["login"] for s in graph_after],
    }
    console.print(Pretty(report["evaluation"]))


def cleanup_move(
    rec: Recorder, gc: GraphClient, sp: SharePointRest, ctx: Ctx, report: dict[str, Any]
) -> None:
    p = ctx.prefix
    _, drive_id, _ = resolve_site_and_drive(rec, gc, ctx.host, ctx.site_path, report)
    delete_folder(rec, gc, drive_id, f"{p}sp-move-src", ctx, report)
    delete_folder(rec, gc, drive_id, f"{p}sp-move-dst", ctx, report)
    delete_groups(gc, [f"{p}probe-move-dst"], ctx, report)
    delete_user(gc, ctx, "a", report)


@app.command("move")
def cmd_move(
    cleanup: bool = CLEANUP_OPT,
    wait: float = WAIT_OPT,
    logon_name: str = typer.Option("upn", help="ensureuser input for the user: 'upn' or 'claim'."),
    spec_path: Path = SPEC_OPT,
) -> None:
    """(d) Move a uniquely-permissioned file into a folder with different unique permissions."""
    if logon_name not in ("upn", "claim"):
        raise typer.BadParameter("must be 'upn' or 'claim'")
    run_command(
        "move", cleanup, partial(work_move, wait=wait, style=logon_name), cleanup_move, spec_path
    )


# ---- e) idempotency -----------------------------------------------------------------------


def member_ref_body(member_id: str) -> dict[str, Any]:
    return {"@odata.id": f"{GRAPH_BASE_URL}/directoryObjects/{member_id}"}


def add_member_step(
    rec: Recorder,
    gc: GraphClient,
    group_id: str,
    member_id: str,
    label: str,
    *,
    retry_404: bool,
    allow: tuple[int, ...] = (),
) -> tuple[int, Any]:
    """POST /groups/{id}/members/$ref. First adds retry 404 (replication); repeats allow errors."""
    status: int = 0
    body: Any = None
    for attempt in range(1, ADD_RETRIES + 1):
        last = attempt == ADD_RETRIES or not retry_404
        status, body = step(
            rec,
            label,
            "POST /groups/{id}/members/$ref",
            lambda: gc.request(
                "POST", f"/groups/{group_id}/members/$ref", json=member_ref_body(member_id)
            ),
            lambda d: "added",
            allow=allow if last else (*allow, 404),
        )
        if status != 404 or last:
            return status, body
        console.print(f"  {label}: HTTP 404, retrying in {ADD_RETRY_WAIT_S:.0f}s")
        time.sleep(ADD_RETRY_WAIT_S)
    return status, body


def work_idempotency(
    rec: Recorder,
    gc: GraphClient,
    sp: SharePointRest,
    ctx: Ctx,
    report: dict[str, Any],
    *,
    wait: float,
) -> None:
    p = ctx.prefix
    console.print("[bold]idempotency: repeat the same role assignment and group-member adds[/bold]")
    _, drive_id, library_url = resolve_site_and_drive(rec, gc, ctx.host, ctx.site_path, report)
    group = ensure_group(gc, f"{p}probe-idem", ctx, 0.0, report)
    member_group = ensure_group(gc, f"{p}probe-idem-b", ctx, 0.0, report)
    user = ensure_probe_user(gc, ctx, "a", wait, report)

    console.print("[bold]Role assignment twice[/bold]")
    folder_name = f"{p}sp-idem-folder"
    create_folder(rec, gc, drive_id, folder_name, ctx)
    item = folder_item_path(f"{library_url}/{folder_name}")
    break_inheritance(rec, sp, item, "break inheritance (folder)")
    principal = group_principal(rec, sp, group["id"], "idempotency group")
    read_id = read_role_id(rec, sp, report)
    first, _ = grant_read(rec, sp, item, int(principal["Id"]), read_id, "addroleassignment (1st)")
    second, second_body = grant_read(
        rec,
        sp,
        item,
        int(principal["Id"]),
        read_id,
        "addroleassignment (2nd, same)",
        allow=ALL_ERRORS,
    )
    after = snapshot(rec, sp, "folder after both adds", item, report)
    same = [a for a in after["assignments"] if a["login"] == principal["LoginName"]]

    console.print("[bold]Group member added twice[/bold]")
    u1, _ = add_member_step(
        rec, gc, group["id"], user["id"], "add user to group (1st)", retry_404=True
    )
    u2, u2_body = add_member_step(
        rec,
        gc,
        group["id"],
        user["id"],
        "add user to group (2nd, same)",
        retry_404=False,
        allow=ALL_ERRORS,
    )
    g1, _ = add_member_step(
        rec, gc, group["id"], member_group["id"], "add group to group (1st)", retry_404=True
    )
    g2, g2_body = add_member_step(
        rec,
        gc,
        group["id"],
        member_group["id"],
        "add group to group (2nd, same)",
        retry_404=False,
        allow=ALL_ERRORS,
    )

    def assumed(status: int, body: Any) -> bool:
        message = str((body or {}).get("message", "")) if isinstance(body, dict) else ""
        return status == 400 and "already exist" in message.lower()

    report["evaluation"] = {
        "role_assignment_status_1st_2nd": [first, second],
        "role_assignment_2nd_body": second_body,
        "assignments_for_group_after_both": len(same),
        "user_add_status_1st_2nd": [u1, u2],
        "user_add_2nd_body": u2_body,
        "group_add_status_1st_2nd": [g1, g2],
        "group_add_2nd_body": g2_body,
        "graph_client_assumption_400_already_exist_holds": {
            "user": assumed(u2, u2_body),
            "group": assumed(g2, g2_body),
        },
    }
    console.print(Pretty(redact_payload(report["evaluation"])))


def cleanup_idempotency(
    rec: Recorder, gc: GraphClient, sp: SharePointRest, ctx: Ctx, report: dict[str, Any]
) -> None:
    p = ctx.prefix
    _, drive_id, _ = resolve_site_and_drive(rec, gc, ctx.host, ctx.site_path, report)
    delete_folder(rec, gc, drive_id, f"{p}sp-idem-folder", ctx, report)
    delete_groups(gc, [f"{p}probe-idem", f"{p}probe-idem-b"], ctx, report)
    delete_user(gc, ctx, "a", report)


@app.command("idempotency")
def cmd_idempotency(
    cleanup: bool = CLEANUP_OPT, wait: float = WAIT_OPT, spec_path: Path = SPEC_OPT
) -> None:
    """(e) Repeat a role assignment and a group-member add; record exact statuses and bodies."""
    run_command(
        "idempotency", cleanup, partial(work_idempotency, wait=wait), cleanup_idempotency, spec_path
    )


if __name__ == "__main__":
    app()
