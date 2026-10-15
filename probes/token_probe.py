"""Probe: can we get tokens, and are we in the test tenant?

What it does, in order (stops at the first failure, exit code 1):
  1. Acquire an app-only Graph token for the driver app, then call assert_test_tenant()
     with that token's `tid` BEFORE anything else happens.
  2. Acquire an app-only SharePoint token (https://{LEAKLAB_SP_HOST}/.default).
  3. With --persona <upn>: acquire a delegated Graph token for that persona.
  4. GET https://graph.microsoft.com/v1.0/organization with the app token (connectivity check).

Output: redacted claims printed with rich and saved to probes/out/token_probe.json.
No token, password or private key is printed or saved; only the claims in config.SAFE_CLAIMS.

Run:  uv run python probes/token_probe.py [--persona ivy@yourtenant.onmicrosoft.com]
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import typer
from rich.console import Console
from rich.pretty import Pretty

from leaklab.auth import GRAPH_DEFAULT_SCOPE, AuthError, get_app_token, get_persona_token, sp_scope
from leaklab.config import assert_test_tenant, redact_jwt

OUT_FILE = Path(__file__).resolve().parent / "out" / "token_probe.json"
ORG_URL = "https://graph.microsoft.com/v1.0/organization"

console = Console()
app = typer.Typer(add_completion=False)


def save(report: dict[str, Any]) -> None:
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    console.print(f"Saved redacted results to {OUT_FILE}")


def fail(report: dict[str, Any], message: str) -> None:
    """Record the failure, save what we have so far, and exit non-zero."""
    report["status"] = "failed"
    report["error"] = message
    console.print(f"[bold red]FAILED:[/bold red] {message}")
    save(report)
    raise typer.Exit(code=1)


def show(report: dict[str, Any], name: str, token: str) -> str:
    """Print and record a token's redacted claims; return its tid (or '' if absent)."""
    claims = redact_jwt(token)
    report["tokens"][name] = claims
    console.print(f"[bold]{name}[/bold] (redacted claims)")
    console.print(Pretty(claims))
    return str(claims.get("tid", ""))


@app.command()
def main(
    persona: str = typer.Option("", help="UPN of one persona user to get a delegated token for."),
    persona_scope: str = typer.Option(
        "https://graph.microsoft.com/User.Read", help="Delegated scope to request for the persona."
    ),
) -> None:
    report: dict[str, Any] = {
        "probe": "token_probe",
        "run_at": datetime.now(UTC).isoformat(),
        "status": "running",
        "tokens": {},
    }
    try:
        # 1. Graph app token, then the safety guard before anything else.
        graph_token = get_app_token(GRAPH_DEFAULT_SCOPE)
        tid = show(report, "app-only Graph", graph_token)
        assert_test_tenant(tid)
        console.print(
            "[green]Tenant guard passed: token tid matches LEAKLAB_ALLOWED_TENANT_ID[/green]"
        )

        # 2. SharePoint app token.
        sp_token = get_app_token(sp_scope())
        assert_test_tenant(show(report, "app-only SharePoint", sp_token))

        # 3. Optional persona token.
        if persona:
            persona_token = get_persona_token(persona, [persona_scope])
            assert_test_tenant(show(report, f"persona {persona} (delegated Graph)", persona_token))
    except (AuthError, RuntimeError, ValueError) as exc:
        fail(report, str(exc))

    # 4. Connectivity check.
    try:
        resp = httpx.get(ORG_URL, headers={"Authorization": f"Bearer {graph_token}"}, timeout=30.0)
    except httpx.HTTPError as exc:
        fail(report, f"GET /organization could not be sent: {type(exc).__name__}")
    report["organization_request"] = {"url": ORG_URL, "status_code": resp.status_code}
    if resp.status_code != 200:
        try:
            err = resp.json().get("error", {})
            detail = f"{err.get('code')}: {err.get('message')}"
        except ValueError:
            detail = "non-JSON response"
        # Verified 2026-10-15: the driver app's four roles are enough for 200 here (no
        # Organization.Read.All). A 403 would mean one of those roles was removed.
        fail(report, f"GET /organization returned HTTP {resp.status_code} ({detail})")
    orgs = resp.json().get("value", [])
    name = orgs[0].get("displayName") if orgs else None
    report["organization_request"]["display_name"] = name
    console.print(f"Tenant display name: [bold]{name}[/bold]")

    report["status"] = "ok"
    save(report)


if __name__ == "__main__":
    app()
