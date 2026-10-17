"""Why did addroleassignment answer HTTP 400 "Value does not fall within the expected range" for
a direct Read grant to ll-p-plateng on S1-Direct, when direct grants to ll-p-ceo and ll-p-hr worked?

It generates the tenant (stopping at the first SharePoint error), then runs a few experiments on the
failing call and prints what each one answered. Nothing is hidden: every answer is saved.
Writes probes/out/user_grant_400_probe_<UTC>.json (no tokens, no secrets). Always tears down.

NOTE: written BEFORE the fix. Since Env.principal no longer caches user ids, `generate` here
succeeds and the experiments no longer reproduce the failure; the saved output of 2026-10-17 is the
evidence (docs/FINDINGS.md).

Run:  uv run python probes/user_grant_400_probe.py
"""

import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from leaklab.generator import generate, teardown
from leaklab.generator.cli import open_env
from leaklab.sp_rest import SPRestError, parse_body

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "probes" / "out"


def attempt(label: str, fn: Any, out: list[dict[str, Any]]) -> None:
    try:
        fn()
        out.append({"step": label, "result": "ok"})
    except SPRestError as exc:
        out.append({"step": label, "result": f"HTTP {exc.status_code}", "body": exc.body})
    print(out[-1], flush=True)


def main() -> None:
    results: list[dict[str, Any]] = []
    with open_env(ROOT / "tenant_spec.yaml") as env:
        try:
            try:
                generate(env)
                results.append({"step": "generate", "result": "ok"})
            except SPRestError as exc:
                results.append(
                    {"step": "generate", "result": f"HTTP {exc.status_code}", "body": exc.body}
                )
            print(results[-1], flush=True)
            raw = env.sp._sp  # type: ignore[attr-defined]
            read = env.role_id("Read")
            direct = env.folder_item("S1-Direct")
            board = env.folder_item("Board")
            for user in ("p-plateng", "p-ceo", "p-intern"):
                upn = next(u.upn for u in env.plan.users if u.id == user)
                data = parse_body(raw.ensure_user(upn))
                results.append({"step": f"ensureuser {user}", "data": data})
                print(results[-1], flush=True)
            plateng = env.principal("user", "p-plateng")
            ceo = env.principal("user", "p-ceo")
            intern = env.principal("user", "p-intern")
            attempt(
                "add p-ceo (known-good user) to S1-Direct",
                lambda: env.sp.add_assignment(direct, ceo.sp_id, read),
                results,
            )
            attempt(
                "add p-plateng to Board (a folder where user grants work)",
                lambda: env.sp.add_assignment(board, plateng.sp_id, read),
                results,
            )
            attempt(
                "add p-intern to S1-Direct",
                lambda: env.sp.add_assignment(direct, intern.sp_id, read),
                results,
            )
            attempt(
                "add p-plateng to S1-Direct (the failing call)",
                lambda: env.sp.add_assignment(direct, plateng.sp_id, read),
                results,
            )
            time.sleep(60)
            attempt(
                "add p-plateng to S1-Direct again after 60 s",
                lambda: env.sp.add_assignment(direct, plateng.sp_id, read),
                results,
            )
            fresh = env.sp.ensure_principal(
                next(u.upn for u in env.plan.users if u.id == "p-plateng")
            )
            results.append({"step": "ensureuser p-plateng again", "data": fresh})
            attempt(
                "add p-plateng to S1-Direct with the re-ensured id",
                lambda: env.sp.add_assignment(direct, int(fresh["Id"]), read),
                results,
            )
        finally:
            results.append({"step": "teardown", "counts": dict(teardown(env).counts)})
            OUT.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            path = OUT / f"user_grant_400_probe_{stamp}.json"
            path.write_text(json.dumps(results, indent=2, default=str) + "\n", encoding="utf-8")
            print("saved", path)


if __name__ == "__main__":
    main()
