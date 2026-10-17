"""Does Graph drive delta report PERMISSION changes? Probe, per CLAUDE.md, before building the ACL store.

Docs (graph/api/driveitem-delta, read 2026-10-17): `Prefer: hierarchicalsharing` returns sharing
information only for the root of a permissions hierarchy and for items with explicit sharing
changes; `Prefer: deltashowsharingchanges` (which REQUIRES `deltashowremovedasdeleted` and
`deltatraversepermissiongaps`; needs Sites.FullControl.All) marks every item that appears because of
a permission change with the annotation `@microsoft.graph.sharedChanged: "True"`.

This probe takes four header sets side by side (A none, B hierarchicalsharing, C the three headers of
the docs, D the three plus hierarchicalsharing), gets a `token=latest` delta link for each, then makes
REAL changes in the test tenant and polls every 5 s, recording exactly which items each header set
returns, with every annotation and facet that appears, and how long after the change.

Changes (each followed by its undo):
  1 remove / add p-plateng's Read grant on the FOLDER S1-Direct (its 2 documents inherit)
  2 remove / add p-hr's Read grant on the special FILE f-direct-share (unique permissions on a file)
  3 remove / add p-plateng in the SharePoint group sp-s1-spgroup
  4 remove / add p-plateng in the Entra group grp-s1-entra
(3 and 4 are membership changes: the docs say nothing about them in a DRIVE delta; we check.)

Output: probes/out/delta_probe_<UTC>.json. The pre-authenticated download URLs Graph puts on file
items are dropped. Test tenant only (open_env runs the tenant guard). Restores the tenant at the end.

Run:  uv run python probes/delta_probe.py [--watch 90]
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

THREE = "deltashowremovedasdeleted, deltatraversepermissiongaps, deltashowsharingchanges"
HEADER_SETS: dict[str, dict[str, str]] = {
    "A_none": {},
    "B_hierarchicalsharing": {"Prefer": "hierarchicalsharing"},
    "C_three_headers": {"Prefer": THREE},
    "D_three_plus_hierarchicalsharing": {"Prefer": THREE + ", hierarchicalsharing"},
}
DROP_KEYS = {"@microsoft.graph.downloadUrl", "@microsoft.graph.downloadUrlNoAuth"}


def clean(item: dict[str, Any]) -> dict[str, Any]:
    """An item as returned, minus pre-authenticated URLs (never saved)."""
    return {k: v for k, v in item.items() if k not in DROP_KEYS}


def mutation(kind: MutationType, **params: Any) -> Mutation:
    return Mutation(type=kind, params=params)


CHANGES: list[tuple[str, Mutation]] = [
    (
        "1a remove p-plateng Read on folder S1-Direct",
        mutation(
            MutationType.GRANT_REMOVED,
            target="S1-Direct",
            principal_kind="user",
            principal_ref="p-plateng",
        ),
    ),  # noqa: E501
    (
        "1b add it back",
        mutation(
            MutationType.GRANT_ADDED,
            target="S1-Direct",
            principal_kind="user",
            principal_ref="p-plateng",
            role="read",
        ),
    ),  # noqa: E501
    (
        "2a remove p-hr Read on file f-direct-share",
        mutation(
            MutationType.GRANT_REMOVED,
            target="special:f-direct-share",
            principal_kind="user",
            principal_ref="p-hr",
        ),
    ),  # noqa: E501
    (
        "2b add it back",
        mutation(
            MutationType.GRANT_ADDED,
            target="special:f-direct-share",
            principal_kind="user",
            principal_ref="p-hr",
            role="read",
        ),
    ),  # noqa: E501
    (
        "3a remove p-plateng from SharePoint group sp-s1-spgroup",
        mutation(
            MutationType.GROUP_MEMBER_REMOVED,
            group="sp-s1-spgroup",
            member_kind="user",
            member_ref="p-plateng",
        ),
    ),  # noqa: E501
    (
        "3b add back",
        mutation(
            MutationType.GROUP_MEMBER_ADDED,
            group="sp-s1-spgroup",
            member_kind="user",
            member_ref="p-plateng",
        ),
    ),  # noqa: E501
    (
        "4a remove p-plateng from Entra group grp-s1-entra",
        mutation(
            MutationType.GROUP_MEMBER_REMOVED,
            group="grp-s1-entra",
            member_kind="user",
            member_ref="p-plateng",
        ),
    ),  # noqa: E501
    (
        "4b add back",
        mutation(
            MutationType.GROUP_MEMBER_ADDED,
            group="grp-s1-entra",
            member_kind="user",
            member_ref="p-plateng",
        ),
    ),  # noqa: E501
]


class Delta:
    """One header set's delta feed over the document library."""

    def __init__(self, gc: GraphClient, drive_id: str, name: str, headers: dict[str, str]) -> None:
        self.gc, self.name, self.headers = gc, name, headers
        self.drive_id = drive_id
        resp = gc.request(
            "GET", f"/drives/{drive_id}/root/delta", params={"token": "latest"}, headers=headers
        )  # noqa: E501
        self.link: str = resp.json()["@odata.deltaLink"]
        self.first_status = resp.status_code

    def poll(self) -> list[dict[str, Any]]:
        """Everything the feed has since the last poll (all pages); advances the delta link."""
        items: list[dict[str, Any]] = []
        url: str | None = self.link
        for _ in range(50):
            resp = self.gc.request("GET", url, headers=self.headers)
            page = resp.json()
            items += [clean(i) for i in page.get("value", [])]
            if "@odata.nextLink" in page:
                url = page["@odata.nextLink"]
                continue
            self.link = page.get("@odata.deltaLink", self.link)
            break
        return items


def summarize(item: dict[str, Any]) -> dict[str, Any]:
    """The interesting part of an item: name, kind, and every annotation / sharing-ish key."""
    keys = sorted(
        k for k in item if k.startswith("@") or k in ("shared", "sharepointIds", "deleted")
    )
    return {
        "name": item.get("name"),
        "kind": "folder" if "folder" in item else "file" if "file" in item else "other",
        "id": item.get("id"),
        "annotations_and_sharing": {k: item[k] for k in keys},
        "all_keys": sorted(item),
    }


@app.command()
def main(watch: float = typer.Option(90.0, help="Seconds to watch after each change.")) -> None:
    out: dict[str, Any] = {
        "started_utc": datetime.now(UTC).isoformat(),
        "header_sets": HEADER_SETS,
        "changes": [],
    }  # noqa: E501
    with open_env(ROOT / "tenant_spec.yaml") as env, GraphClient() as gc:
        try:
            generate(env)  # start from the spec state
            time.sleep(60)  # let Entra and SharePoint settle
            feeds = [Delta(gc, env.drive_id, n, h) for n, h in HEADER_SETS.items()]
            out["initial_status"] = {f.name: f.first_status for f in feeds}
            for f in feeds:  # drain anything the generate left behind
                f.poll()
            for label, change in CHANGES:
                record: dict[str, Any] = {"change": label, "per_header_set": {}}
                out["changes"].append(record)
                t0 = time.time()
                entry = apply_mutation(env, change)
                record["applied"] = {
                    "status": entry.status,
                    "confirmed_after_s": round((entry.confirmed_at or t0) - t0, 2),
                    "note": entry.note,
                }  # noqa: E501
                console.print(f"[bold]{label}[/bold] -> {entry.status}")
                seen: dict[str, list[dict[str, Any]]] = {f.name: [] for f in feeds}
                first: dict[str, float] = {}
                deadline = time.time() + watch
                while time.time() < deadline:
                    for f in feeds:
                        got = f.poll()
                        if got:
                            first.setdefault(f.name, round(time.time() - t0, 1))
                            seen[f.name] += got
                    time.sleep(5)
                for f in feeds:
                    items = seen[f.name]
                    record["per_header_set"][f.name] = {
                        "first_seen_after_s": first.get(f.name),
                        "item_count": len(items),
                        "items": [summarize(i) for i in items][:20],
                        "sharedChanged_true": [
                            i.get("name")
                            for i in items
                            if i.get("@microsoft.graph.sharedChanged") in (True, "True", "true")
                        ],  # noqa: E501
                    }
                    console.print(
                        f"   {f.name}: {len(items)} item(s), first after {first.get(f.name)} s, sharedChanged on {record['per_header_set'][f.name]['sharedChanged_true']}"
                    )  # noqa: E501
        finally:
            out["restore"] = dict(generate(env).counts)
            OUT.mkdir(parents=True, exist_ok=True)
            path = OUT / f"delta_probe_{datetime.now(UTC):%Y%m%dT%H%M%SZ}.json"
            path.write_text(json.dumps(out, indent=2, default=str) + "\n", encoding="utf-8")
            console.print(f"saved {path}")


if __name__ == "__main__":
    app()
