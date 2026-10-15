"""Probe: do nested Entra groups show up in transitiveMemberOf, and how fast?

Default run (creates objects; idempotent, existing ones are reused):
  1. assert_test_tenant() on the app token's tid, before anything else.
  2. Create users "<prefix>probe-a", "<prefix>probe-b" and security groups
     "<prefix>probe-outer", "<prefix>probe-mid", "<prefix>probe-inner"
     (prefix and domain come from tenant_spec.yaml; password = LEAKLAB_PERSONA_PASSWORD).
  3. Nest inner -> mid -> outer and put probe-a in inner.
  4. Poll GET /users/{probe-a}/transitiveMemberOf every --interval seconds (max --timeout)
     until all three groups appear; record how long that took. probe-b (in no group) is
     checked once as a negative control.
  5. Save redacted results (ids, names, timings; never passwords or tokens) to
     probes/out/graph_probe.json.

With --cleanup: ONLY deletes the five probe objects (names must start with
"<prefix>probe-") and exits; nothing is created. Its report goes to
probes/out/graph_probe_cleanup.json so it never overwrites the probe results.

Findings are recorded by hand in docs/FINDINGS.md; this script asserts nothing about what
Graph should do. It exits 0 even if the groups never became visible (that is a finding),
and 1 on errors.

Run:  uv run python probes/graph_probe.py [--domain yourtenant.onmicrosoft.com] [--cleanup]
"""

import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import typer
from rich.console import Console

from leaklab.auth import GRAPH_DEFAULT_SCOPE, AuthError, get_app_token
from leaklab.config import assert_test_tenant, get_settings, redact_jwt
from leaklab.graph_client import GraphClient, GraphError
from leaklab.spec import load_spec

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "probes" / "out"
# Separate files so a --cleanup run can never overwrite the probe results.
OUT_FILES = {"probe": OUT_DIR / "graph_probe.json", "cleanup": OUT_DIR / "graph_probe_cleanup.json"}
USER_NAMES = ("probe-a", "probe-b")
GROUP_NAMES = ("probe-outer", "probe-mid", "probe-inner")
# Newly created directory objects may not be addable to a group immediately (replication).
# Verified 2026-10-15: a new group returned HTTP 404 for ~10s before it could be nested.
# Each retry is recorded in the output.
ADD_RETRIES = 6
ADD_RETRY_WAIT_S = 5.0

console = Console()
app = typer.Typer(add_completion=False)


def save(report: dict[str, Any]) -> None:
    out_file = OUT_FILES[report["mode"]]
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    console.print(f"Saved redacted results to {out_file}")


def fail(report: dict[str, Any], message: str) -> None:
    report["status"] = "failed"
    report["error"] = message
    console.print(f"[bold red]FAILED:[/bold red] {message}")
    save(report)
    raise typer.Exit(code=1)


def slim(obj: dict[str, Any]) -> dict[str, Any]:
    """Keep only non-secret identifying fields of a directory object."""
    keep = ("id", "displayName", "userPrincipalName")
    return {k: obj[k] for k in keep if k in obj}


def assert_probe_name(name: str, prefix: str) -> None:
    """Safety: we only ever touch objects named "<prefix>probe-...". Anything else aborts."""
    if not name.startswith(f"{prefix}probe-"):
        raise RuntimeError(f"Refusing to touch {name!r}: not a probe object.")


def add_with_retry(add: Any, what: str, report: dict[str, Any]) -> bool:
    """Call `add()`; retry on HTTP 404 (object not replicated yet), recording each retry."""
    for attempt in range(1, ADD_RETRIES + 1):
        try:
            added: bool = add()
            report["memberships"].append({"what": what, "added": added, "attempts": attempt})
            console.print(f"  {what}: {'added' if added else 'already a member'}")
            return added
        except GraphError as exc:
            if exc.status_code != 404 or attempt == ADD_RETRIES:
                raise
            console.print(f"  {what}: HTTP 404, retrying in {ADD_RETRY_WAIT_S:.0f}s")
            time.sleep(ADD_RETRY_WAIT_S)
    raise AssertionError("unreachable")  # pragma: no cover


def run_cleanup(client: GraphClient, prefix: str, domain: str, report: dict[str, Any]) -> None:
    report["mode"] = "cleanup"
    report["deleted"] = []
    for short in USER_NAMES:  # users first, then groups
        name = f"{prefix}{short}"
        assert_probe_name(name, prefix)
        user = client.get_user_by_upn(f"{name}@{domain}")
        if user is None:
            console.print(f"user {name}: not found")
            continue
        client.delete_user(user["id"])
        report["deleted"].append({"type": "user", **slim(user)})
        console.print(f"user {name}: deleted")
    for short in GROUP_NAMES:
        name = f"{prefix}{short}"
        assert_probe_name(name, prefix)
        group = client.find_group_by_name(name)
        if group is None:
            console.print(f"group {name}: not found")
            continue
        client.delete_group(group["id"])
        report["deleted"].append({"type": "group", **slim(group)})
        console.print(f"group {name}: deleted")


def run_probe(
    client: GraphClient,
    prefix: str,
    domain: str,
    password: str,
    timeout: float,
    interval: float,
    report: dict[str, Any],
) -> None:
    report["mode"] = "probe"
    report["memberships"] = []
    users: dict[str, dict[str, Any]] = {}
    groups: dict[str, dict[str, Any]] = {}

    console.print("[bold]1. Users and groups[/bold]")
    for short in USER_NAMES:
        name = f"{prefix}{short}"
        assert_probe_name(name, prefix)
        upn = f"{name}@{domain}"
        found = client.get_user_by_upn(upn)
        state = "reused"
        if found is None:
            found = client.create_user(upn, name, name, password)
            state = "created"
        users[short] = found
        report.setdefault("users", {})[short] = {**slim(found), "state": state}
        console.print(f"  user {name}: {state}")
    for short in GROUP_NAMES:
        name = f"{prefix}{short}"
        assert_probe_name(name, prefix)
        found = client.find_group_by_name(name)
        state = "reused"
        if found is None:
            found = client.create_security_group(name, name)
            state = "created"
        groups[short] = found
        report.setdefault("groups", {})[short] = {**slim(found), "state": state}
        console.print(f"  group {name}: {state}")

    console.print("[bold]2. Nesting: inner -> mid -> outer, probe-a in inner[/bold]")
    add_with_retry(
        lambda: client.add_group_to_group(groups["probe-mid"]["id"], groups["probe-inner"]["id"]),
        "inner in mid",
        report,
    )
    add_with_retry(
        lambda: client.add_group_to_group(groups["probe-outer"]["id"], groups["probe-mid"]["id"]),
        "mid in outer",
        report,
    )
    add_with_retry(
        lambda: client.add_user_to_group(groups["probe-inner"]["id"], users["probe-a"]["id"]),
        "probe-a in inner",
        report,
    )

    console.print("[bold]3. Polling transitiveMemberOf for probe-a[/bold]")
    wanted = {groups[s]["id"]: s for s in GROUP_NAMES}
    polls: list[dict[str, Any]] = []
    report["polls"] = polls
    start = time.monotonic()
    visible_after: float | None = None
    while True:
        returned = client.list_transitive_member_of(users["probe-a"]["id"])
        elapsed = time.monotonic() - start
        seen = sorted(wanted[g["id"]] for g in returned if g["id"] in wanted)
        polls.append(
            {
                "elapsed_s": round(elapsed, 1),
                "probe_groups_seen": seen,
                "total_returned": len(returned),
            }
        )
        console.print(f"  t={elapsed:5.1f}s  seen {len(seen)}/3: {', '.join(seen) or '-'}")
        if len(seen) == len(wanted):
            visible_after = elapsed
            break
        if elapsed + interval > timeout:
            break
        time.sleep(interval)
    report["all_three_visible_after_s"] = None if visible_after is None else round(visible_after, 1)
    if visible_after is None:
        console.print(f"[yellow]Not all three groups visible within {timeout:.0f}s[/yellow]")
    else:
        console.print(f"[green]All three groups visible after {visible_after:.1f}s[/green]")

    console.print("[bold]4. Negative control: probe-b[/bold]")
    control = client.list_transitive_member_of(users["probe-b"]["id"])
    leaked = sorted(wanted[g["id"]] for g in control if g["id"] in wanted)
    report["control_probe_b_probe_groups"] = leaked
    console.print(f"  probe-b sees probe groups: {leaked or 'none (as expected)'}")


@app.command()
def main(
    cleanup: bool = typer.Option(False, "--cleanup", help="Only delete the probe objects."),
    domain: str = typer.Option("", help="Tenant domain; overrides naming.domain in the spec."),
    spec_path: Path = typer.Option(ROOT / "tenant_spec.yaml", help="Path to tenant_spec.yaml."),
    timeout: float = typer.Option(120.0, help="Max seconds to wait for nested membership."),
    interval: float = typer.Option(5.0, help="Seconds between polls."),
) -> None:
    report: dict[str, Any] = {
        "probe": "graph_probe",
        "run_at": datetime.now(UTC).isoformat(),
        "status": "running",
        "mode": "cleanup" if cleanup else "probe",
    }
    try:
        spec = load_spec(spec_path)
        prefix = spec.naming.prefix
        tenant_domain = domain or spec.naming.domain
        if "YOURTENANT" in tenant_domain.upper():
            raise ValueError(
                "naming.domain is still the placeholder. Pass --domain <tenant>.onmicrosoft.com "
                "or fix tenant_spec.yaml."
            )
        report.update(prefix=prefix, domain=tenant_domain)

        # Safety guard first, using the tid of the token we are about to use.
        tid = str(redact_jwt(get_app_token(GRAPH_DEFAULT_SCOPE)).get("tid", ""))
        assert_test_tenant(tid)
        console.print("[green]Tenant guard passed[/green]")

        password = get_settings().persona_password.get_secret_value()
        if not cleanup and not password:
            raise ValueError("LEAKLAB_PERSONA_PASSWORD is not set; needed to create users.")

        with GraphClient() as client:
            if cleanup:
                run_cleanup(client, prefix, tenant_domain, report)
            else:
                run_probe(client, prefix, tenant_domain, password, timeout, interval, report)
    except (AuthError, GraphError, RuntimeError, ValueError, OSError, httpx.HTTPError) as exc:
        fail(report, f"{type(exc).__name__}: {exc}")

    report["status"] = "ok"
    save(report)


if __name__ == "__main__":
    app()
