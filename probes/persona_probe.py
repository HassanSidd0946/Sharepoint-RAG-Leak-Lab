"""Probe: can persona users sign in end to end (create -> license -> token -> /me)?

Steps (stops at the first hard failure; nothing is worked around):
  0. assert_test_tenant() on the app token's tid, before anything else.
  1. Create (reuse if they exist) the users p-intern and p-plateng from tenant_spec.yaml,
     with the persona password and usageLocation from LEAKLAB_USAGE_LOCATION. If a user is
     `licensed: true`, discover the license SKU with GET /subscribedSkus (never hardcoded) and
     assign it. If more than one SKU is a candidate, the probe stops and asks for
     --sku-part-number.
  2. For each user, acquire a delegated Graph token with auth.get_persona_token
     (username/password, device code DISABLED). Known "not propagated yet" errors are retried
     with backoff for up to --signin-timeout seconds; every error code seen and the time to the
     first success are recorded. Any other error (MFA, consent, public-client, ...) stops the
     probe and the exact error code and description are printed.
  3. Print the token's redacted claims and call GET /me with it; check /me is that user.

Output: probes/out/persona_probe.json (redacted: no tokens, no passwords).
--cleanup deletes only these two users and exits (report: persona_probe_cleanup.json).

Run:  uv run python probes/persona_probe.py [--sku-part-number <part>] [--cleanup]
Needs in .env: LEAKLAB_USAGE_LOCATION (e.g. US) in addition to the existing settings.
"""

import json
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import typer
from rich.console import Console
from rich.markup import escape
from rich.pretty import Pretty
from rich.table import Table

from leaklab.auth import (
    GRAPH_DEFAULT_SCOPE,
    AuthError,
    PersonaSignInError,
    get_app_token,
    get_persona_token,
)
from leaklab.config import assert_test_tenant, get_settings, redact_jwt
from leaklab.graph_client import GraphClient, GraphError
from leaklab.spec import TenantSpec, load_spec, user_mail_nickname, user_upn

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "probes" / "out"
OUT_FILES = {
    "probe": OUT_DIR / "persona_probe.json",
    "cleanup": OUT_DIR / "persona_probe_cleanup.json",
}
TARGET_IDS = ("p-intern", "p-plateng")
SCOPES = ["https://graph.microsoft.com/User.Read"]
# UNVERIFIED: which error codes a brand-new user returns before it can sign in. On the first run
# (2026-10-15) no error occurred at all, so these are still guesses. We retry only these two
# (user not found yet / credentials not valid yet) and record every code we see.
RETRYABLE_CODES: frozenset[int] = frozenset({50034, 50126})
FIRST_WAIT_S = 5.0
MAX_WAIT_S = 30.0

console = Console()
app = typer.Typer(add_completion=False)


class ProbeStop(Exception):
    """A step failed in a way we must not work around. Carries what to print and save."""

    def __init__(self, message: str, detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.detail = detail or {}


def save(report: dict[str, Any]) -> None:
    out_file = OUT_FILES[report["mode"]]
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    console.print(f"Saved redacted results to {out_file}")


def bearer(token: str) -> Callable[[], str]:
    return lambda: token


def sku_row(sku: dict[str, Any]) -> dict[str, Any]:
    prepaid = sku.get("prepaidUnits", {}) or {}
    enabled = int(prepaid.get("enabled", 0))
    consumed = int(sku.get("consumedUnits", 0))
    return {
        "skuPartNumber": sku.get("skuPartNumber"),
        "skuId": sku.get("skuId"),
        "appliesTo": sku.get("appliesTo"),
        "capabilityStatus": sku.get("capabilityStatus"),
        "enabledUnits": enabled,
        "consumedUnits": consumed,
        "availableUnits": enabled - consumed,
    }


def choose_sku(skus: list[dict[str, Any]], part_number: str, report: dict[str, Any]) -> str:
    """Pick the license SKU id. Auto-pick only if exactly one SKU is a candidate."""
    rows = [sku_row(s) for s in skus]
    report["subscribed_skus"] = rows
    if part_number:
        matches = [r for r in rows if r["skuPartNumber"] == part_number]
        if len(matches) != 1:
            raise ProbeStop(
                f"--sku-part-number {part_number!r} matched {len(matches)} SKUs.", {"skus": rows}
            )
        chosen = matches[0]
    else:
        candidates = [
            r
            for r in rows
            if r["appliesTo"] == "User"
            and r["capabilityStatus"] == "Enabled"
            and r["availableUnits"] > 0
        ]
        if len(candidates) != 1:
            raise ProbeStop(
                f"{len(candidates)} SKUs could be assigned; rerun with --sku-part-number "
                "<skuPartNumber of the Business Basic SKU>. Candidates are listed above.",
                {"candidates": candidates},
            )
        chosen = candidates[0]
    report["chosen_sku"] = chosen
    console.print(f"License SKU: {chosen['skuPartNumber']} ({chosen['availableUnits']} available)")
    return str(chosen["skuId"])


def ensure_user(
    gc: GraphClient,
    spec: TenantSpec,
    user_id: str,
    password: str,
    usage_location: str,
    sku_id: str | None,
    report: dict[str, Any],
) -> dict[str, Any]:
    """Create or reuse the user, set usageLocation, assign the license. Returns a record."""
    user = next(u for u in spec.users if u.id == user_id)
    upn = user_upn(spec, user_id)
    nickname = user_mail_nickname(spec, user_id)
    assert nickname.startswith(spec.naming.prefix)
    record: dict[str, Any] = {"spec_id": user_id, "upn": upn, "licensed_in_spec": user.licensed}

    found = gc.get_user_by_upn(upn)
    if found is None:
        found = gc.create_user(upn, user.display, nickname, password, usage_location)
        record["state"] = "created"
    else:
        record["state"] = "reused"
    record["object_id"] = found["id"]
    record["created_at_monotonic"] = time.monotonic()
    console.print(f"user {upn}: {record['state']}")

    licensing = gc.get_user_licensing(found["id"])
    if licensing.get("usageLocation") != usage_location:
        gc.update_user(found["id"], {"usageLocation": usage_location})
        record["usage_location_set"] = True
    if user.licensed:
        if sku_id is None:
            raise ProbeStop("internal: no SKU chosen for a licensed user")
        assigned = {lic.get("skuId") for lic in licensing.get("assignedLicenses", [])}
        if sku_id in assigned:
            record["license"] = "already assigned"
        else:
            gc.assign_license(found["id"], sku_id)
            record["license"] = "assign requested"
        after = gc.get_user_licensing(found["id"])
        record["assigned_sku_ids_after"] = [
            lic.get("skuId") for lic in after.get("assignedLicenses", [])
        ]
        console.print(f"  license: {record['license']}")
    return record


def wait_for_sign_in(upn: str, started: float, timeout_s: float, record: dict[str, Any]) -> str:
    """Retry username/password sign-in on propagation errors; stop on anything else."""
    attempts: list[dict[str, Any]] = []
    record["sign_in_attempts"] = attempts
    delay = FIRST_WAIT_S
    while True:
        elapsed = time.monotonic() - started
        try:
            token = get_persona_token(upn, SCOPES, allow_device_code=False)
        except PersonaSignInError as exc:
            retryable = bool(exc.error_codes) and all(c in RETRYABLE_CODES for c in exc.error_codes)
            attempts.append(
                {
                    "elapsed_s": round(elapsed, 1),
                    "error": exc.error,
                    "error_codes": exc.error_codes,
                    "retryable": retryable,
                    "description": exc.description,
                }
            )
            console.print(
                f"  t={elapsed:5.1f}s  sign-in error {exc.error} {exc.error_codes}"
                f"{' (retrying)' if retryable else ''}"
            )
            if not retryable:
                raise ProbeStop(
                    "Sign-in failed with an error we do not retry.",
                    {
                        "upn": upn,
                        "error": exc.error,
                        "error_codes": exc.error_codes,
                        "description": exc.description,
                        "interaction_required": exc.interaction_required,
                    },
                ) from exc
            if elapsed + delay > timeout_s:
                raise ProbeStop(
                    f"Not able to sign in within {timeout_s:.0f}s.",
                    {"upn": upn, "last_error_codes": exc.error_codes, "last": exc.description},
                ) from exc
            time.sleep(delay)
            delay = min(delay * 1.5, MAX_WAIT_S)
            continue
        attempts.append({"elapsed_s": round(elapsed, 1), "ok": True})
        record["time_to_sign_in_s"] = round(elapsed, 1)
        record["error_codes_seen"] = sorted({c for a in attempts for c in a.get("error_codes", [])})
        console.print(f"  signed in after {elapsed:.1f}s")
        return token


def check_me(token: str, user: dict[str, Any]) -> None:
    """Print redacted claims, call GET /me, and verify it is the expected user."""
    claims = redact_jwt(token)
    user["claims"] = claims
    console.print(Pretty({k: claims.get(k) for k in ("tid", "oid", "upn", "scp") if k in claims}))
    with GraphClient(bearer(token)) as me_client:
        resp = me_client.request("GET", "/me", params={"$select": "id,userPrincipalName"})
    me = resp.json()
    user["me"] = {
        "status": resp.status_code,
        "id": me.get("id"),
        "upn": me.get("userPrincipalName"),
    }
    same = (
        str(me.get("userPrincipalName", "")).lower() == user["upn"].lower()
        and me.get("id") == user["object_id"]
    )
    user["me"]["matches_expected_user"] = same
    console.print(f"  GET /me -> {resp.status_code}, matches expected user: {same}")
    if not same:
        raise ProbeStop("GET /me returned a different identity than expected.", user["me"])


def run_probe(
    spec: TenantSpec, sku_part_number: str, timeout_s: float, report: dict[str, Any]
) -> None:
    settings = get_settings()
    password = settings.persona_password.get_secret_value()
    location = settings.usage_location.strip().upper()
    if not password:
        raise ProbeStop("LEAKLAB_PERSONA_PASSWORD is not set.")
    if len(location) != 2 or not location.isalpha():
        raise ProbeStop("LEAKLAB_USAGE_LOCATION must be a two-letter country code, e.g. US.")
    targets = [u for u in spec.users if u.id in TARGET_IDS]
    if len(targets) != len(TARGET_IDS) or not all(u.persona for u in targets):
        raise ProbeStop("tenant_spec.yaml must contain persona users " + ", ".join(TARGET_IDS))

    report["usage_location"] = location
    records: list[dict[str, Any]] = []
    report["users"] = records
    with GraphClient() as gc:
        sku_id: str | None = None
        if any(u.licensed for u in targets):
            console.print("[bold]1. License SKU[/bold]")
            sku_id = choose_sku(gc.list_subscribed_skus(), sku_part_number, report)
        console.print("[bold]2. Users[/bold]")
        for user_id in TARGET_IDS:
            records.append(ensure_user(gc, spec, user_id, password, location, sku_id, report))

    console.print("[bold]3. Sign-in[/bold]")
    for record in records:
        console.print(f"{record['upn']}")
        token = wait_for_sign_in(record["upn"], record["created_at_monotonic"], timeout_s, record)
        check_me(token, record)
    for record in records:
        record.pop("created_at_monotonic", None)


def run_cleanup(spec: TenantSpec, report: dict[str, Any]) -> None:
    report["deleted"] = []
    with GraphClient() as gc:
        for user_id in TARGET_IDS:
            nickname = user_mail_nickname(spec, user_id)
            if not nickname.startswith(spec.naming.prefix):
                raise ProbeStop(f"Refusing to delete {nickname!r}: no prefix.")
            upn = user_upn(spec, user_id)
            found = gc.get_user_by_upn(upn)
            if found is None:
                console.print(f"user {upn}: not found")
                continue
            gc.delete_user(found["id"])
            report["deleted"].append({"upn": upn, "id": found["id"]})
            console.print(f"user {upn}: deleted")


def print_summary(report: dict[str, Any]) -> None:
    table = Table(title="persona_probe summary")
    for col in ("User", "State", "License", "Sign-in (s)", "Error codes seen", "/me"):
        table.add_column(col, overflow="fold")
    for u in report.get("users", []):
        table.add_row(
            u["upn"],
            u.get("state", ""),
            u.get("license", "n/a"),
            str(u.get("time_to_sign_in_s", "-")),
            ", ".join(str(c) for c in u.get("error_codes_seen", [])) or "none",
            str(u.get("me", {}).get("status", "-")),
        )
    console.print(table)


@app.command()
def main(
    cleanup: bool = typer.Option(False, "--cleanup", help="Only delete the two persona users."),
    sku_part_number: str = typer.Option("", help="License SKU part number (if not unique)."),
    signin_timeout: float = typer.Option(300.0, help="Max seconds to wait for sign-in."),
    spec_path: Path = typer.Option(ROOT / "tenant_spec.yaml", help="Path to tenant_spec.yaml."),
) -> None:
    report: dict[str, Any] = {
        "probe": "persona_probe",
        "run_at": datetime.now(UTC).isoformat(),
        "status": "running",
        "mode": "cleanup" if cleanup else "probe",
    }
    try:
        spec = load_spec(spec_path)
        # Safety guard first.
        assert_test_tenant(str(redact_jwt(get_app_token(GRAPH_DEFAULT_SCOPE)).get("tid", "")))
        console.print("[green]Tenant guard passed[/green]")
        if cleanup:
            run_cleanup(spec, report)
        else:
            run_probe(spec, sku_part_number, signin_timeout, report)
    except ProbeStop as exc:
        report["status"] = "failed"
        report["stopped"] = {"message": str(exc), **exc.detail}
        for u in report.get("users", []):
            u.pop("created_at_monotonic", None)
        console.print(f"[bold red]STOPPED:[/bold red] {escape(str(exc))}")
        for key, value in exc.detail.items():
            console.print(f"{key}: ", end="")
            console.print(Pretty(value))
        save(report)
        raise typer.Exit(code=1) from exc
    except (AuthError, GraphError, RuntimeError, ValueError, OSError, httpx.HTTPError) as exc:
        report["status"] = "failed"
        report["error"] = f"{type(exc).__name__}: {exc}"
        for u in report.get("users", []):
            u.pop("created_at_monotonic", None)
        console.print(f"[bold red]FAILED:[/bold red] {escape(report['error'])}")
        save(report)
        raise typer.Exit(code=1) from exc

    report["status"] = "ok"
    print_summary(report)
    save(report)


if __name__ == "__main__":
    app()
