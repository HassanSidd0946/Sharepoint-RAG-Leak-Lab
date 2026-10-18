"""Probe for the index cleanup (docs/FINDINGS.md, "Index cleanup"): count, delete one, clean up.

Stages (each saves probes/out/index_cleanup_<stage>_<UTC>.json; no secrets, no tokens):
  count       how many documents each index holds, how many of them belong to the CURRENT tenant
              generation (45 expected), how many are stale. ll-sp-index-b is listed with the service
              token; ll-sp-index (ACL-filtered) cannot be listed that way: only its two counts are
              recorded (the search count is 0 for the service token; documentCount is a LAGGING
              statistic).
  delete-one  deletes ONE stale key (taken from ll-sp-index-b) from both indexes and checks what the
              service reports: that the delete call is accepted while permission filtering is on,
              that
              the document is gone from ll-sp-index-b at once, and how ll-sp-index's
              documentCount moves.
  cleanup     the full cleanup (setup_a/cleanup.cleanup_indexes) with before/after counts.

Test tenant only (guard_test_tenant). Deletes only keys whose Graph item id is not in the live
tenant.
Run: uv run python probes/index_cleanup_probe.py count | delete-one | cleanup
"""

import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import typer
from rich.console import Console

from leaklab.config import get_settings
from leaklab.generator.cli import open_env
from leaklab.graph_client import GraphClient
from leaklab.runner.tenant import resolve_item_ids, saved_endpoint
from leaklab.setup_a.cleanup import (
    GenerationRegistry,
    HttpIndexAdmin,
    cleanup_indexes,
    default_registry_path,
    split_keys,
)
from leaklab.setup_a.client import INDEX
from leaklab.setup_b.api.search import INDEX_B

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "probes" / "out"
SPEC_PATH = ROOT / "tenant_spec.yaml"
console = Console()
app = typer.Typer(add_completion=False, no_args_is_help=True)


def endpoint() -> str:
    found = get_settings().search_endpoint.strip() or saved_endpoint()
    if not found:
        raise typer.BadParameter("no search endpoint (LEAKLAB_SEARCH_ENDPOINT)")
    return found


def save(stage: str, data: dict[str, Any]) -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = OUT / f"index_cleanup_{stage}_{stamp}.json"
    path.write_text(json.dumps(data, indent=2, default=str) + "\n", encoding="utf-8")
    console.print(f"saved {path}")
    return path


def tenant_facts() -> tuple[str, set[str], str, str]:
    """(generation, current item ids, site id, drive id) of the live tenant."""
    settings = get_settings()
    site_url = settings.site_url.strip()
    with open_env(SPEC_PATH) as env, GraphClient() as gc:
        canaries = [c for f in env.plan.folders for d in f.docs for c in [d.canary]]
        canaries += [s.doc.canary for s in env.plan.special_files]
        ids = set(resolve_item_ids(env, canaries).values())
        site = gc.get(f"/sites/{settings.sp_host.strip()}:{urlsplit(site_url).path.rstrip('/')}")
        return env.generation, ids, str(site["id"]), env.drive_id


def admins() -> tuple[HttpIndexAdmin, HttpIndexAdmin]:
    return HttpIndexAdmin(endpoint(), INDEX_B), HttpIndexAdmin(endpoint(), INDEX)


@app.command("count")
def count() -> None:
    generation, current, _, _ = tenant_facts()
    unfiltered, filtered = admins()
    keys = unfiltered.list_keys()
    kept, stale, foreign = split_keys(keys, current)
    data = {
        "generation": generation,
        "current_item_ids": len(current),
        INDEX_B: {
            "search_count": unfiltered.count_search(),
            "documentCount_stat": unfiltered.count_stats(),
            "listed": len(keys),
            "current": len(kept),
            "stale": len(stale),
            "foreign": len(foreign),
            "distinct_item_ids": len({k for k in (kk.split("::")[-1] for kk in keys)}),
        },
        INDEX: {
            "search_count_with_service_token": filtered.count_search(),
            "documentCount_stat": filtered.count_stats(),
            "stale_estimate_from_stat": (filtered.count_stats() or 0) - len(current),
            "note": "documentCount is a lagging statistic; the ACL-filtered index cannot be listed "
            "with the service token",
        },
    }
    console.print(data)
    save("count", data)


@app.command("delete-one")
def delete_one(wait_s: float = 240.0) -> None:
    generation, current, _, _ = tenant_facts()
    unfiltered, filtered = admins()
    keys = unfiltered.list_keys()
    _, stale, _ = split_keys(keys, current)
    if not stale:
        console.print("no stale key in ll-sp-index-b: nothing to probe")
        return
    key = stale[0]
    a_before, b_before = filtered.count_stats(), unfiltered.count_search()
    result_b = unfiltered.delete_keys([key])
    result_a = filtered.delete_keys([key])
    time.sleep(3)
    still_there = key in unfiltered.list_keys()
    b_after = unfiltered.count_search()
    series: list[tuple[float, int | None]] = []
    started = time.time()
    while time.time() - started < wait_s:
        series.append((round(time.time() - started), filtered.count_stats()))
        if series[-1][1] is not None and a_before is not None and series[-1][1] < a_before:
            break
        time.sleep(15)
    data = {
        "generation": generation,
        "deleted_key_is_current": key in {k for k in keys if k not in stale},
        "delete_in_index_b": result_b.model_dump(),
        "delete_in_index_a": result_a.model_dump(),
        "index_b": {
            "count_before": b_before,
            "count_after": b_after,
            "key_still_listed": still_there,
        },
        "index_a_documentCount_stat": {"before": a_before, "series_s_count": series},
    }
    console.print(data)
    save("delete_one", data)


@app.command("cleanup")
def cleanup(dry_run: bool = False) -> None:
    generation, current, site_id, drive_id = tenant_facts()
    unfiltered, filtered = admins()
    registry = GenerationRegistry(default_registry_path())
    registry.register(generation, current)
    report = cleanup_indexes(
        unfiltered=unfiltered,
        filtered=filtered,
        generation=generation,
        current_item_ids=current,
        site_id=site_id,
        drive_id=drive_id,
        registry=registry,
        dry_run=dry_run,
    )
    console.print(report.model_dump())
    save("cleanup_dry" if dry_run else "cleanup", report.model_dump())


if __name__ == "__main__":
    app()
