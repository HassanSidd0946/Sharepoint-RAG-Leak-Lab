"""Verify generator.apply_mutation on the REAL test tenant (no scenario runner involved).

Steps: fresh generate + verify -> apply S4 (move f-direct-share into Board), S1 (remove
grp-engineering's grant on Engineering), S2 (remove grp-eng-platform from grp-engineering), one at a
time -> teardown (always, even after a failure).

For each mutation it records requested/executed/confirmed_at (epoch seconds), re-checks the result
with its OWN REST/Entra reads (not the check used inside apply_mutation), and prints the oracle's
expected access per persona before and after, taken from log.oracle_mutations(). It also reports
which retry rules fired (Env records a "retry" event) and whether newly created users really ended
up with a license (re-read from Graph, not trusted from the generator's event).

Writes a TIMESTAMPED file probes/out/mutation_probe_<UTC>.json (never overwrites). No secrets are
written: only UPNs, group names, ids of tenant objects and canary strings.

Run:  uv run python probes/mutation_probe.py
"""

import json
import time
import traceback
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from leaklab.generator import generate, teardown, verify
from leaklab.generator.cli import open_env
from leaklab.generator.env import LIMITED_ACCESS, Env
from leaklab.generator.generate import choose_sku
from leaklab.generator.mutate import apply_mutation
from leaklab.oracle import Mutation, MutationType, expected_access
from leaklab.spec import generate_canaries

ROOT = Path(__file__).resolve().parents[1]
SPEC_PATH = ROOT / "tenant_spec.yaml"
OUT_DIR = ROOT / "probes" / "out"
POLL_TIMEOUT_S = 90.0
POLL_INTERVAL_S = 3.0


def poll(check: Callable[[], dict[str, Any]], consecutive: int = 2) -> dict[str, Any]:
    """Call check() until it says ok `consecutive` times in a row, or POLL_TIMEOUT_S passes."""
    deadline = time.time() + POLL_TIMEOUT_S
    streak = 0
    last: dict[str, Any] = {}
    while True:
        try:
            last = check()
        except Exception as exc:  # a failed read counts as "not confirmed yet"
            last = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        streak = streak + 1 if last.get("ok") else 0
        if streak >= consecutive:
            return {**last, "checked_at": time.time()}
        if time.time() >= deadline:
            return {**last, "ok": False, "timed_out": True}
        time.sleep(POLL_INTERVAL_S)


def real_grants(env: Env, item: str) -> list[tuple[str, str]]:
    """(login, role) pairs on an item, without SharePoint's own Limited Access entries."""
    return sorted(
        (a.login, r.name)
        for a in env.sp.assignments(item)
        for r in a.roles
        if r.kind != 1 and r.name != LIMITED_ACCESS
    )


def oracle_view(env: Env, at: float) -> dict[str, list[str]]:
    """persona -> sorted canary keys it should read at `at`, from the mutation log."""
    canaries = generate_canaries(env.spec)
    by_canary = {c: key for key, cs in canaries.items() for c in cs}
    exp = expected_access(env.spec, env.log.oracle_mutations(), at, canaries=canaries)
    return {p: sorted(by_canary[c] for c in cs) for p, cs in sorted(exp.items())}


def diff_views(before: dict[str, list[str]], after: dict[str, list[str]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for persona in sorted(set(before) | set(after)):
        b, a = set(before.get(persona, [])), set(after.get(persona, []))
        if b != a:
            out[persona] = {"lost": sorted(b - a), "gained": sorted(a - b)}
    return out


def mutations_to_apply() -> list[tuple[str, Mutation]]:
    return [
        (
            "S4-move-into-restricted",
            Mutation(
                type=MutationType.FILE_MOVED,
                params={"file": "f-direct-share", "to_folder": "Board"},
            ),
        ),
        (
            "S1-parent-revocation",
            Mutation(
                type=MutationType.GRANT_REMOVED,
                params={
                    "target": "Engineering",
                    "principal_kind": "entra_group",
                    "principal_ref": "grp-engineering",
                },
            ),
        ),
        (
            "S2-nested-group-removal",
            Mutation(
                type=MutationType.GROUP_MEMBER_REMOVED,
                params={
                    "group": "grp-engineering",
                    "member_kind": "entra_group",
                    "member_ref": "grp-eng-platform",
                },
            ),
        ),
    ]


def recheck_s4(env: Env, grants_before: list[tuple[str, str]]) -> dict[str, Any]:
    """The file is in Board (Graph), gone from General (Graph), and kept its own grants (REST)."""
    name = env.special_file_name("f-direct-share")
    board, general = env.folder_id("Board"), env.folder_id("General")
    assert board is not None and general is not None
    item = env.item_path("special:f-direct-share")

    def check() -> dict[str, Any]:
        in_board = env.graph.find_item(env.drive_id, board, name) is not None
        in_general = env.graph.find_item(env.drive_id, general, name) is not None
        unique = env.sp.has_unique(item)
        grants = real_grants(env, item)
        return {
            "ok": in_board and not in_general and unique and grants == grants_before,
            "in_board": in_board,
            "in_general": in_general,
            "has_unique_permissions": unique,
            "grants_before_move": grants_before,
            "grants_after_move": grants,
            "rest_item_path": item,
        }

    return poll(check)


def recheck_s1(env: Env) -> dict[str, Any]:
    """Engineering has no real grant for grp-engineering any more; its sibling scopes are intact."""
    login = env.principal("entra_group", "grp-engineering").login
    plat_login = env.principal("entra_group", "grp-eng-platform").login

    def check() -> dict[str, Any]:
        eng = real_grants(env, env.item_path("Engineering"))
        plat = real_grants(env, env.item_path("Engineering/Platform-Secrets"))
        return {
            "ok": all(lg != login for lg, _ in eng) and plat == [(plat_login, "Read")],
            "engineering_grants_now": eng,
            "platform_secrets_grants_now": plat,
            "engineering_still_unique": env.sp.has_unique(env.item_path("Engineering")),
        }

    return poll(check)


def recheck_s2(env: Env) -> dict[str, Any]:
    """The user inside grp-eng-platform is no longer (transitively) in grp-engineering, but a
    direct member of grp-engineering still is."""
    eng, plat = env.group_id("grp-engineering"), env.group_id("grp-eng-platform")
    p_plateng, u_eng1 = env.user_id("p-plateng"), env.user_id("u-eng1")

    def check() -> dict[str, Any]:
        g_plat = env.graph.user_group_ids(p_plateng)
        g_eng1 = env.graph.user_group_ids(u_eng1)
        return {
            "ok": eng not in g_plat and plat in g_plat and eng in g_eng1,
            "p_plateng_in_grp_engineering": eng in g_plat,
            "p_plateng_in_grp_eng_platform": plat in g_plat,
            "u_eng1_in_grp_engineering": eng in g_eng1,
        }

    return poll(check)


def license_check(env: Env, created_upns: list[str]) -> list[dict[str, Any]]:
    sku_id = choose_sku(env.graph.list_skus(), env.settings.sku_part_number)
    licensed = {u.upn for u in env.plan.users if u.licensed}
    rows = []
    for upn in sorted(licensed):
        user_id = str(env.graph.get_user(upn)["id"])  # type: ignore[index]
        result = poll(lambda uid=user_id: {"ok": sku_id in env.graph.user_license_sku_ids(uid)})
        rows.append(
            {
                "upn": upn,
                "newly_created_in_this_run": upn in created_upns,
                "has_license_in_graph": bool(result["ok"]),
            }
        )
    return rows


def main() -> None:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out: dict[str, Any] = {"started_utc": stamp, "steps": [], "errors": []}
    path = OUT_DIR / f"mutation_probe_{stamp}.json"

    def save() -> None:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(out, indent=2, default=str) + "\n", encoding="utf-8")

    with open_env(SPEC_PATH) as env:
        try:
            report = generate(env)
            out["generate_counts"] = dict(report.counts)
            out["generate_warnings"] = list(report.warnings)
            print("generate:", dict(report.counts))
            vrep = verify(env)
            out["verify_ok"] = vrep.ok
            out["verify_issues"] = [i.model_dump() for i in vrep.issues]
            print("verify ok:", vrep.ok, [i.what for i in vrep.issues])
            save()
            if not vrep.ok:
                raise RuntimeError("verify failed after generate; not applying mutations")

            created = [e["upn"] for e in env.events if e["event"] == "user_created"]
            out["licenses"] = license_check(env, created)
            print("licenses:", out["licenses"])
            save()

            for scenario, mutation in mutations_to_apply():
                step: dict[str, Any] = {"scenario": scenario}
                out["steps"].append(step)
                events_before = len(env.events)
                before = oracle_view(env, time.time())
                grants_before = (
                    real_grants(env, env.item_path("special:f-direct-share"))
                    if mutation.type is MutationType.FILE_MOVED
                    else []
                )
                try:
                    entry = apply_mutation(env, mutation)
                except Exception as exc:
                    step["error"] = f"{type(exc).__name__}: {exc}"
                    step["log_entry"] = (
                        env.log.entries[-1].model_dump(mode="json") if env.log.entries else None
                    )
                    print(scenario, "FAILED:", step["error"])
                    save()
                    continue
                step["log_entry"] = entry.model_dump(mode="json")
                step["confirm_latency_s"] = (
                    None
                    if entry.confirmed_at is None or entry.executed_at is None
                    else round(entry.confirmed_at - entry.executed_at, 2)
                )
                if scenario.startswith("S4"):
                    step["recheck"] = recheck_s4(env, grants_before)
                elif scenario.startswith("S1"):
                    step["recheck"] = recheck_s1(env)
                else:
                    step["recheck"] = recheck_s2(env)
                after_t = entry.confirmed_at if entry.confirmed_at is not None else time.time()
                after = oracle_view(env, after_t)
                step["oracle_before"] = before
                step["oracle_after"] = after
                step["oracle_diff"] = diff_views(before, after)
                step["events"] = env.events[events_before:]
                print(
                    scenario,
                    entry.status,
                    "confirmed_at",
                    entry.confirmed_at,
                    "latency",
                    step["confirm_latency_s"],
                    "recheck_ok",
                    step["recheck"]["ok"],
                )
                print("  oracle diff:", json.dumps(step["oracle_diff"]))
                save()
        except Exception:
            out["errors"].append(traceback.format_exc())
            print(traceback.format_exc())
        finally:
            retries = [e for e in env.events if e["event"] == "retry"]
            summary: dict[str, int] = {}
            for e in retries:
                k = f"{e['rule']} (status {e['status']})"
                summary[k] = summary.get(k, 0) + 1
            out["retries_fired"] = summary
            out["oracle_mutations"] = [
                m.model_dump(mode="json") for m in env.log.oracle_mutations()
            ]
            try:
                out["teardown_counts"] = dict(teardown(env).counts)
                print("teardown:", out["teardown_counts"])
            except Exception:
                out["errors"].append("teardown failed:\n" + traceback.format_exc())
                print(traceback.format_exc())
            save()
            print("retries fired:", summary or "none")
            print("saved:", path)


if __name__ == "__main__":
    main()
