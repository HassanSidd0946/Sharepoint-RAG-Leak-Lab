"""What does Graph's GROUP delta report for membership changes, and how fast? Probe before building
the membership poller (setup_b/state/membership_store).

Docs (graph/api/group-delta, read 2026-10-17): `GET /groups/delta?$select=members`; the response
carries `members@delta` (member `@odata.type` and `id`); only the id of the related object is
returned; Prefer: return=minimal returns only changed properties on a deltaLink call. NOT stated:
the shape of a REMOVED member, whether nested-group changes surface on the parent, the latency.

Takes two feeds side by side (default, and Prefer: return=minimal), drains the initial state, then
makes REAL membership changes (each followed by its undo) and polls every 5 s:
  1 p-plateng out of / back into grp-s1-entra                 (user in a group)
  2 grp-eng-platform out of / back into grp-engineering       (group in a group: S2)
  3 p-plateng out of / back into grp-eng-platform             (user in the CHILD group: does the
                                                               PARENT group appear in the feed?)
Records every group object returned (name, `members@delta` entries with all their keys, any
`@removed`), and the delay from the change to the first appearance. Output
probes/out/group_delta_probe_<UTC>.json. Restores the tenant at the end. Test tenant only.

Run:  uv run python probes/group_delta_probe.py [--watch 60]
"""

import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import typer
from rich.console import Console

from leaklab.generator import generate
from leaklab.generator.cli import open_env
from leaklab.generator.mutate import apply_mutation
from leaklab.graph_client import GraphClient
from leaklab.oracle import Mutation, MutationType

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "probes" / "out"
console = Console()
app = typer.Typer(add_completion=False)

FEEDS: dict[str, dict[str, str]] = {"default": {}, "return_minimal": {"Prefer": "return=minimal"}}


def member(kind: MutationType, group: str, member_kind: str, ref: str) -> Mutation:
    return Mutation(
        type=kind, params={"group": group, "member_kind": member_kind, "member_ref": ref}
    )


R, A = MutationType.GROUP_MEMBER_REMOVED, MutationType.GROUP_MEMBER_ADDED
CHANGES: list[tuple[str, Mutation]] = [
    ("1a remove p-plateng from grp-s1-entra", member(R, "grp-s1-entra", "user", "p-plateng")),
    ("1b add back", member(A, "grp-s1-entra", "user", "p-plateng")),
    (
        "2a remove grp-eng-platform from grp-engineering",
        member(R, "grp-engineering", "entra_group", "grp-eng-platform"),
    ),  # noqa: E501
    ("2b add back", member(A, "grp-engineering", "entra_group", "grp-eng-platform")),
    (
        "3a remove p-plateng from grp-eng-platform",
        member(R, "grp-eng-platform", "user", "p-plateng"),
    ),  # noqa: E501
    ("3b add back", member(A, "grp-eng-platform", "user", "p-plateng")),
]


class Feed:
    def __init__(self, gc: GraphClient, name: str, headers: dict[str, str]) -> None:
        self.gc, self.name, self.headers = gc, name, headers
        self.initial: list[dict[str, Any]] = []
        url: str | None = "/groups/delta"
        params: dict[str, str] | None = {"$select": "displayName,members"}
        self.link = ""
        for _ in range(100):
            assert url is not None
            resp = gc.request("GET", url, params=params, headers=headers)
            page = resp.json()
            self.initial += page.get("value", [])
            params = None
            if "@odata.nextLink" in page:
                url = page["@odata.nextLink"]
                continue
            self.link = page["@odata.deltaLink"]
            break

    def poll(self) -> list[dict[str, Any]]:
        got: list[dict[str, Any]] = []
        url: str | None = self.link
        for _ in range(50):
            assert url is not None
            page = self.gc.request("GET", url, headers=self.headers).json()
            got += page.get("value", [])
            if "@odata.nextLink" in page:
                url = page["@odata.nextLink"]
                continue
            self.link = page.get("@odata.deltaLink", self.link)
            break
        return got


def brief(group: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": group.get("id"),
        "displayName": group.get("displayName"),
        "keys": sorted(group),
        "removed": group.get("@removed"),
        "members_delta": group.get("members@delta"),
    }


@app.command()
def main(watch: float = typer.Option(60.0, help="Seconds to watch after each change.")) -> None:
    out: dict[str, Any] = {"started_utc": datetime.now(UTC).isoformat(), "changes": []}
    with open_env(ROOT / "tenant_spec.yaml") as env, GraphClient() as gc:
        try:
            generate(env)
            time.sleep(45)
            feeds = [Feed(gc, n, h) for n, h in FEEDS.items()]
            ours = {env.group_id(g.id): g.id for g in env.plan.groups}
            out["initial_groups_total"] = len(feeds[0].initial)
            out["initial_our_groups"] = [brief(g) for g in feeds[0].initial if g.get("id") in ours][
                :10
            ]
            for f in feeds:
                f.poll()
            for label, change in CHANGES:
                record: dict[str, Any] = {"change": label, "feeds": {}}
                out["changes"].append(record)
                t0 = time.time()
                entry = apply_mutation(env, change)
                record["applied"] = {
                    "status": entry.status,
                    "confirmed_after_s": round((entry.confirmed_at or t0) - t0, 2),
                }  # noqa: E501
                console.print(f"[bold]{label}[/bold] -> {entry.status}")
                seen: dict[str, list[dict[str, Any]]] = {f.name: [] for f in feeds}
                first: dict[str, float] = {}
                while time.time() - t0 < watch:
                    for f in feeds:
                        got = [g for g in f.poll() if g.get("id") in ours]
                        if got:
                            first.setdefault(f.name, round(time.time() - t0, 1))
                            seen[f.name] += got
                    time.sleep(5)
                for f in feeds:
                    record["feeds"][f.name] = {
                        "first_seen_after_s": first.get(f.name),
                        "our_groups_returned": [ours[g["id"]] for g in seen[f.name]],
                        "objects": [brief(g) for g in seen[f.name]][:6],
                    }
                    console.print(
                        f"   {f.name}: first after {first.get(f.name)} s, groups {record['feeds'][f.name]['our_groups_returned']}"
                    )  # noqa: E501
        finally:
            out["restore"] = dict(generate(env).counts)
            OUT.mkdir(parents=True, exist_ok=True)
            path = OUT / f"group_delta_probe_{datetime.now(UTC):%Y%m%dT%H%M%SZ}.json"
            path.write_text(json.dumps(out, indent=2, default=str) + "\n", encoding="utf-8")
            console.print(f"saved {path}")


if __name__ == "__main__":
    app()
