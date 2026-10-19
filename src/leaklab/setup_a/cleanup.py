"""Remove the documents of earlier tenant generations from the two search indexes.

Why: every teardown + generate makes a new tenant with new drive item ids, and neither indexer
deletes the documents of the tenant that is gone. After a few generations an index holds several
copies of every document (measured 2026-10-18 in ll-sp-index-b: stale copies of old generations).
They are never served, but they fill result windows and make "is this document indexed?" ambiguous.

What is deleted: every document whose key does NOT belong to the CURRENT generation. A key encodes
the Graph item id (keys.py), so "current" is a set of item ids taken from the tenant.

  ll-sp-index-b   (Setup B)  readable with the service token: list every key, delete the stale ones
  ll-sp-index     (Setup A)  ACL-filtered: the service token sees 0 documents, so its keys cannot be
                  listed. Stale keys come from (1) the registry of item ids of earlier generations
                  (register_generation, written at every run) and (2) the stale keys found in
                  ll-sp-index-b (the same items, the same key derivation). An optional elevated
                  listing (an admin's user token) can be passed for a one-time historic cleanup.

Deleting a key that does not exist is not an error (the service reports success), so an
over-inclusive key list is harmless. Current keys are never in the delete list.

HTTP: POST /indexes/{index}/docs/index with {"value": [{"@search.action": "delete", "id": key}]},
at most 1000 actions per call, with the driver app's bearer (role Search Index Data Contributor).
Counts: `count_search` (an exact search count: only meaningful for an unfiltered index) and
`count_stats` (documentCount of the index statistics: a LAGGING number, see docs/FINDINGS.md).
"""

import json
import time
from collections.abc import Callable, Collection, Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import httpx
from pydantic import BaseModel

from leaklab.auth import describe_token, get_app_token
from leaklab.setup_a.client import API, INDEX, SEARCH_SCOPE
from leaklab.setup_a.keys import doc_key, item_id_of_key
from leaklab.setup_b.api.search import INDEX_B

BATCH = 500
PAGE = 1000


class IndexAdmin(Protocol):
    index: str

    def count_search(self) -> int | None: ...

    def count_stats(self) -> int | None: ...

    def list_keys(self) -> list[str]:
        """Every key the caller can see (all for an unfiltered index, none for a filtered one)."""
        ...

    def delete_keys(self, keys: Sequence[str]) -> "DeleteResult": ...


class DeleteResult(BaseModel):
    requested: int
    succeeded: int
    failed: int
    errors: list[str] = []


class HttpIndexAdmin:
    """The data plane of one index, with the driver app's bearer."""

    def __init__(
        self,
        endpoint: str,
        index: str,
        *,
        client: httpx.Client | None = None,
        token_source: Callable[[], str] = lambda: get_app_token(SEARCH_SCOPE),
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._endpoint = endpoint.rstrip("/")
        self.index = index
        self._http = client or httpx.Client(timeout=120.0)
        self._token_source = token_source
        self._clock = clock
        self._bearer: tuple[str, float] | None = None

    def _headers(self) -> dict[str, str]:
        if self._bearer is None or self._bearer[1] - 300 < self._clock():
            token = self._token_source()
            exp = describe_token(token).get("exp")
            expires = float(exp) if isinstance(exp, int | float) else self._clock() + 600
            self._bearer = (token, expires)
        return {"Authorization": f"Bearer {self._bearer[0]}"}

    def _call(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        resp = self._http.request(
            method,
            f"{self._endpoint}/indexes/{self.index}{path}",
            params={"api-version": API},
            json=body,
            headers=self._headers(),
        )
        if resp.status_code != 200:
            raise RuntimeError(f"{method} {self.index}{path} -> HTTP {resp.status_code}")
        data = resp.json()
        if not isinstance(data, dict):
            raise RuntimeError(f"{method} {self.index}{path}: unexpected answer")
        return data

    def count_search(self) -> int | None:
        data = self._call("POST", "/docs/search", {"search": "*", "count": True, "top": 0})
        count = data.get("@odata.count")
        return count if isinstance(count, int) else None

    def count_stats(self) -> int | None:
        count = self._call("GET", "/stats").get("documentCount")
        return count if isinstance(count, int) else None

    def list_keys(self) -> list[str]:
        keys: list[str] = []
        while True:
            data = self._call(
                "POST",
                "/docs/search",
                {"search": "*", "select": "id", "top": PAGE, "skip": len(keys)},
            )
            page = [str(d["id"]) for d in data.get("value", []) if "id" in d]
            keys += page
            if len(page) < PAGE:
                return keys

    def delete_keys(self, keys: Sequence[str]) -> DeleteResult:
        ok = failed = 0
        errors: list[str] = []
        for start in range(0, len(keys), BATCH):
            batch = keys[start : start + BATCH]
            data = self._call(
                "POST",
                "/docs/index",
                {"value": [{"@search.action": "delete", "id": k} for k in batch]},
            )
            for row in data.get("value", []):
                if row.get("status"):
                    ok += 1
                else:
                    failed += 1
                    errors.append(f"{row.get('key')}: {row.get('errorMessage')}")
        return DeleteResult(requested=len(keys), succeeded=ok, failed=failed, errors=errors[:10])

    def close(self) -> None:
        self._http.close()


def item_of(key: str) -> str | None:
    """The Graph item id behind an index key, or None if the key is not one of ours."""
    try:
        return item_id_of_key(key)
    except ValueError:
        return None


def split_keys(
    keys: Iterable[str], current_item_ids: Collection[str]
) -> tuple[list[str], list[str], list[str]]:
    """(current, stale, foreign): keys of the current generation, keys of earlier ones, and keys
    that are not library documents at all (never deleted)."""
    current_set = set(current_item_ids)
    current: list[str] = []
    stale: list[str] = []
    foreign: list[str] = []
    for key in keys:
        item = item_of(key)
        if item is None:
            foreign.append(key)
        elif item in current_set:
            current.append(key)
        else:
            stale.append(key)
    return current, stale, foreign


class GenerationRegistry:
    """runs/generations.json: the item ids of every tenant generation a run has used.

    This is how an index that cannot be listed (ll-sp-index) is cleaned: the ids of earlier
    generations are known from here. The file only ever grows; it holds ids, no secrets.
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"generations": {}}
        data = json.loads(self.path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) and "generations" in data else {"generations": {}}

    def register(self, generation: str, item_ids: Iterable[str]) -> None:
        data = self._load()
        known = set(data["generations"].get(generation, {}).get("item_ids", []))
        merged = sorted(known | set(item_ids))
        data["generations"][generation] = {
            "registered_utc": data["generations"]
            .get(generation, {})
            .get("registered_utc", datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")),
            "item_ids": merged,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self.path)

    def other_item_ids(self, generation: str) -> set[str]:
        return {
            item
            for gen, entry in self._load()["generations"].items()
            if gen != generation
            for item in entry.get("item_ids", [])
        }

    def generations(self) -> list[str]:
        return sorted(self._load()["generations"])


class IndexReport(BaseModel):
    index: str
    strategy: str
    count_search_before: int | None = None
    count_stats_before: int | None = None
    listed: int | None = None  # keys visible to the caller (None: the index cannot be listed)
    current_found: int | None = None
    foreign_left: int = 0
    stale_found: int = 0
    stale_requested: int = 0
    deleted: int = 0
    failed: int = 0
    count_search_after: int | None = None
    count_stats_after: int | None = None
    errors: list[str] = []


class CleanupReport(BaseModel):
    generation: str
    current_item_ids: int
    registry_generations: int
    indexes: list[IndexReport]


def _counts(admin: IndexAdmin) -> tuple[int | None, int | None]:
    out: list[int | None] = []
    for fn in (admin.count_search, admin.count_stats):
        try:
            out.append(fn())
        except Exception:  # a count is information, never a reason to stop
            out.append(None)
    return out[0], out[1]


def _settled_counts(
    admin: IndexAdmin,
    expected: int,
    settle_s: float,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
) -> tuple[int | None, int | None]:
    """The counts after a delete: the service applies deletes a moment later (a count read right
    after the call still shows the old number), so poll until a count equals `expected` or
    `settle_s` has passed. Returns the last reading."""
    deadline = clock() + settle_s
    while True:
        search, stats = _counts(admin)
        if expected in (search, stats) or clock() >= deadline:
            return search, stats
        sleep(3.0)


def cleanup_indexes(
    *,
    unfiltered: IndexAdmin,
    filtered: IndexAdmin,
    generation: str,
    current_item_ids: Collection[str],
    site_id: str,
    drive_id: str,
    registry: GenerationRegistry,
    elevated_keys: Sequence[str] | None = None,
    dry_run: bool = False,
    settle_s: float = 45.0,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.time,
) -> CleanupReport:
    """Delete the documents of earlier generations from both indexes (see the module docstring).

    `unfiltered` is ll-sp-index-b, `filtered` ll-sp-index. `elevated_keys`: the keys of the filtered
    index from an elevated read, if someone with an admin token listed them (historic cleanup).
    Nothing is deleted if `current_item_ids` is empty: that would mean the tenant could not be read.
    """
    if not current_item_ids:
        raise ValueError("no current item ids: refusing to delete anything")
    current = set(current_item_ids)
    reports: list[IndexReport] = []

    # -- the unfiltered index: list, split, delete the stale ones
    b = IndexReport(index=unfiltered.index, strategy="list the keys with the service token")
    b.count_search_before, b.count_stats_before = _counts(unfiltered)
    listed = unfiltered.list_keys()
    kept, stale_b, foreign = split_keys(listed, current)
    b.listed, b.current_found, b.stale_found, b.foreign_left = (
        len(listed),
        len(kept),
        len(stale_b),
        len(foreign),
    )
    b.stale_requested = len(stale_b)
    if stale_b and not dry_run:
        result = unfiltered.delete_keys(stale_b)
        b.deleted, b.failed, b.errors = result.succeeded, result.failed, result.errors
    b.count_search_after, b.count_stats_after = (
        _counts(unfiltered)
        if dry_run or not stale_b
        else _settled_counts(unfiltered, len(kept), settle_s, sleep, clock)
    )
    reports.append(b)

    # -- the filtered index: keys from the registry, from the unfiltered index and (optionally) an
    # elevated listing; the current generation's keys are never in the list
    a = IndexReport(
        index=filtered.index,
        strategy="registry + stale keys of the unfiltered index"
        + (" + elevated listing" if elevated_keys is not None else ""),
    )
    a.count_search_before, a.count_stats_before = _counts(filtered)
    wanted = {doc_key(site_id, drive_id, item) for item in registry.other_item_ids(generation)}
    wanted |= set(stale_b)
    if elevated_keys is not None:
        _, stale_elevated, _ = split_keys(elevated_keys, current)
        wanted |= set(stale_elevated)
        a.listed = len(elevated_keys)
    current_keys = {doc_key(site_id, drive_id, item) for item in current}
    wanted -= current_keys
    a.stale_found = a.stale_requested = len(wanted)
    if wanted and not dry_run:
        result = filtered.delete_keys(sorted(wanted))
        a.deleted, a.failed, a.errors = result.succeeded, result.failed, result.errors
    a.count_search_after, a.count_stats_after = (
        _counts(filtered)
        if dry_run or not wanted
        else _settled_counts(filtered, len(current), settle_s, sleep, clock)
    )
    reports.append(a)

    return CleanupReport(
        generation=generation,
        current_item_ids=len(current),
        registry_generations=len(registry.generations()),
        indexes=reports,
    )


def default_registry_path() -> Path:
    return Path(__file__).resolve().parents[3] / "runs" / "generations.json"


__all__ = [
    "INDEX",
    "INDEX_B",
    "CleanupReport",
    "DeleteResult",
    "GenerationRegistry",
    "HttpIndexAdmin",
    "IndexAdmin",
    "IndexReport",
    "cleanup_indexes",
    "default_registry_path",
    "split_keys",
]
