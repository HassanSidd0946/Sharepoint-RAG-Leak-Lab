"""Create (or find) the Azure AI Search service for the Setup A probes, and give the driver app
data-plane roles on it. Idempotent: running it again finds what exists and changes nothing.

Why a probe and not setup_a/: CLAUDE.md says probe first. This only provisions the service; what the
service DOES with SharePoint ACLs is for probes/search_acl_probe.py.

Authentication: the driver app (certificate) has NO Azure role, so this signs in as the tenant admin
with a device code (nothing is stored; you approve one sign-in in a browser). The token is for Azure
Resource Manager (scope https://management.azure.com/user_impersonation); it is never printed or
saved.

What it creates (Azure bills Basic by the hour while the service exists; delete the resource group
when you are done):
  - resource group  <--resource-group>            (default leaklab-rg)
  - search service  leaklab-search-<8 hex>        sku basic, 1 replica, 1 partition,
                    system-assigned managed identity, auth = API key OR Entra (aadOrApiKey)
  - role assignments for the driver app's service principal on the service:
    Search Service Contributor, Search Index Data Contributor, Search Index Data Reader
    (looked up by NAME at run time, not hardcoded ids)

Writes runs/search_service.json (git-ignored: endpoint, names, ids) and a timestamped
probes/out/search_provision_<UTC>.json with the names and states only (subscription id shortened).
No keys are read or written: later probes use Entra tokens, not the admin key.

Run:  uv run python probes/search_provision.py [--subscription ID] [--location eastus]
"""

import hashlib
import json
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import typer
from rich.console import Console

from leaklab.auth import GRAPH_DEFAULT_SCOPE, describe_token, get_app_token, get_persona_token
from leaklab.config import assert_test_tenant, get_settings

ROOT = Path(__file__).resolve().parents[1]
ARM = "https://management.azure.com"
ARM_SCOPE = "https://management.azure.com/user_impersonation"
SEARCH_API = "2023-11-01"
ROLES = ("Search Service Contributor", "Search Index Data Contributor", "Search Index Data Reader")
console = Console()
app = typer.Typer(add_completion=False)


class Arm:
    def __init__(self, token: str) -> None:
        self._http = httpx.Client(timeout=60.0, headers={"Authorization": f"Bearer {token}"})

    def call(
        self,
        method: str,
        path: str,
        api: str,
        body: dict[str, Any] | None = None,
        ok: tuple[int, ...] = (200, 201, 202),
    ) -> httpx.Response:
        sep = "&" if "?" in path else "?"
        resp = self._http.request(method, f"{ARM}{path}{sep}api-version={api}", json=body)
        if resp.status_code not in ok:
            raise RuntimeError(f"{method} {path} -> HTTP {resp.status_code}: {resp.text[:600]}")
        return resp


@app.command()
def main(
    subscription: str = typer.Option("", help="Subscription id (needed if the admin has several)."),
    resource_group: str = typer.Option("leaklab-rg"),
    location: str = typer.Option("eastus"),
) -> None:
    settings = get_settings()
    assert_test_tenant(
        settings.tenant_id
    )  # the tenant the sign-in will be for must be the test tenant
    console.print("[bold]Sign in as the admin (device code) for Azure Resource Manager.[/bold]")
    token = get_persona_token(
        settings.admin_upn, [ARM_SCOPE], use_password=False, allow_device_code=True, use_cache=False
    )
    claims = describe_token(token)
    assert_test_tenant(str(claims.get("tid", "")))
    arm = Arm(token)
    out: dict[str, Any] = {"started_utc": datetime.now(UTC).isoformat(), "steps": []}

    subs = arm.call("GET", "/subscriptions", "2022-12-01").json().get("value", [])
    out["subscriptions_visible"] = [
        {
            "id": s["subscriptionId"][:8] + "...",
            "name": s.get("displayName"),
            "state": s.get("state"),
        }
        for s in subs
    ]
    enabled = [s for s in subs if s.get("state") == "Enabled"]
    if subscription:
        enabled = [s for s in enabled if s["subscriptionId"] == subscription]
    if len(enabled) != 1:
        raise SystemExit(
            f"Need exactly one enabled subscription, found {len(enabled)}: "
            f"{out['subscriptions_visible']}. Pass --subscription."
        )
    sub = enabled[0]["subscriptionId"]
    name = "leaklab-search-" + hashlib.sha256(sub.encode()).hexdigest()[:8]
    out["resource_group"], out["service"], out["location"] = resource_group, name, location
    console.print(f"subscription {sub[:8]}... ({enabled[0].get('displayName')}), service {name}")

    # the Microsoft.Search provider must be registered in the subscription
    state = arm.call("GET", f"/subscriptions/{sub}/providers/Microsoft.Search", "2021-04-01").json()
    out["provider_state"] = state.get("registrationState")
    if state.get("registrationState") != "Registered":
        arm.call("POST", f"/subscriptions/{sub}/providers/Microsoft.Search/register", "2021-04-01")
        for _ in range(40):
            time.sleep(5)
            state = arm.call(
                "GET", f"/subscriptions/{sub}/providers/Microsoft.Search", "2021-04-01"
            ).json()
            if state.get("registrationState") == "Registered":
                break
        out["provider_state_after"] = state.get("registrationState")

    arm.call(
        "PUT",
        f"/subscriptions/{sub}/resourcegroups/{resource_group}",
        "2021-04-01",
        {"location": location},
    )
    svc_path = (
        f"/subscriptions/{sub}/resourceGroups/{resource_group}"
        f"/providers/Microsoft.Search/searchServices/{name}"
    )
    existing = arm.call("GET", svc_path, SEARCH_API, ok=(200, 404))
    if existing.status_code == 404:
        console.print("creating the search service (Basic) ...")
        arm.call(
            "PUT",
            svc_path,
            SEARCH_API,
            {
                "location": location,
                "sku": {"name": "basic"},
                "identity": {"type": "SystemAssigned"},
                "properties": {
                    "replicaCount": 1,
                    "partitionCount": 1,
                    "hostingMode": "default",
                    "publicNetworkAccess": "enabled",
                    "authOptions": {
                        "aadOrApiKey": {"aadAuthFailureMode": "http401WithBearerChallenge"}
                    },
                },
            },
        )
        out["created"] = True
    else:
        out["created"] = False
    service: dict[str, Any] = {}
    for _ in range(120):
        service = arm.call("GET", svc_path, SEARCH_API).json()
        if service.get("properties", {}).get("provisioningState") == "succeeded":
            break
        time.sleep(10)
    props = service.get("properties", {})
    out["provisioningState"] = props.get("provisioningState")
    out["sku"] = service.get("sku")
    out["identity_principal_id"] = service.get("identity", {}).get("principalId")
    out["authOptions"] = props.get("authOptions")
    if props.get("provisioningState") != "succeeded":
        raise SystemExit(f"service state is {props.get('provisioningState')!r}")
    endpoint = f"https://{name}.search.windows.net"

    driver_oid = str(describe_token(get_app_token(GRAPH_DEFAULT_SCOPE)).get("oid", ""))
    out["driver_app_object_id_known"] = bool(driver_oid)
    out["role_assignments"] = []
    for role in ROLES:
        defs = (
            arm.call(
                "GET",
                f"/subscriptions/{sub}/providers/Microsoft.Authorization/roleDefinitions"
                f"?$filter=roleName eq '{role}'",
                "2022-04-01",
            )
            .json()
            .get("value", [])
        )
        if len(defs) != 1:
            raise SystemExit(f"role {role!r} matched {len(defs)} definitions")
        assignment = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{svc_path}|{driver_oid}|{role}"))
        resp = arm.call(
            "PUT",
            f"{svc_path}/providers/Microsoft.Authorization/roleAssignments/{assignment}",
            "2022-04-01",
            {
                "properties": {
                    "roleDefinitionId": defs[0]["id"],
                    "principalId": driver_oid,
                    "principalType": "ServicePrincipal",
                }
            },
            ok=(200, 201, 409),
        )
        out["role_assignments"].append({"role": role, "http": resp.status_code})
        console.print(f"role {role}: HTTP {resp.status_code}")

    (ROOT / "runs").mkdir(exist_ok=True)
    (ROOT / "runs" / "search_service.json").write_text(
        json.dumps(
            {
                "endpoint": endpoint,
                "service": name,
                "resource_group": resource_group,
                "subscription_id": sub,
                "location": location,
                "identity_principal_id": out["identity_principal_id"],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = ROOT / "probes" / "out" / f"search_provision_{stamp}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, indent=2, default=str) + "\n", encoding="utf-8")
    console.print(f"[green]endpoint {endpoint}[/green]\nsaved {path}")


if __name__ == "__main__":
    app()
