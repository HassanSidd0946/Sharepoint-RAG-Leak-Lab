"""Setup B's view of the search index: ll-sp-index-b, queried with the SERVICE identity only.

ll-sp-index-b is an UNFILTERED copy of ll-sp-index (same data source, same fields, only
permissionFilterOption disabled; docs/FINDINGS.md, Prompt 19b), so nothing here is trimmed:
Setup B's own post-filter is the only gate. The service token (the driver app's app-only
token) therefore exists ONLY inside this module and the process that owns it; it never goes
into a response, a log, a decision record or the runner's adapter.

Hit.doc_id is the Graph drive item id, derived from the index key (setup_a/keys.py: empirical, the
indexer appends one character). A hit whose key cannot be mapped is DROPPED and counted (we cannot
ask the state about it, so it cannot be shown). UserIds/GroupIds of the index are only ever returned
by `acl_fields` for /explain's comparison; no decision uses them.

HttpIndexPort never raises for an operational problem it can classify: it raises IndexUnavailable
(the API then answers 503, never a partial result).
"""

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from leaklab.auth import describe_token, get_app_token
from leaklab.setup_a.client import API, SEARCH_SCOPE, IndexerRun, parse_run
from leaklab.setup_a.keys import item_id_of_key

INDEX_B = "ll-sp-index-b"
INDEXER_B = "ll-sp-indexer-b"
MAX_TOP = 1000
ACL_CACHE_S = 30.0


class IndexUnavailable(Exception):
    """The index could not be queried (transport error, non-200, bad answer)."""


@dataclass(frozen=True)
class Hit:
    doc_id: str
    name: str
    content: str


@dataclass(frozen=True)
class Fetched:
    hits: list[Hit]
    unmapped: int = 0  # hits dropped because their key could not be mapped to a drive item id
    total_match_count: int | None = None


@dataclass(frozen=True)
class IndexAcl:
    """What the index stored (a stale snapshot). For /explain's comparison only."""

    user_ids: list[str] = field(default_factory=list)
    group_ids: list[str] = field(default_factory=list)


class IndexPort(Protocol):
    def query(self, text: str, top: int, skip: int = 0) -> Fetched: ...

    def stored_acl(self, doc_id: str) -> IndexAcl | None: ...

    def indexer_runs(self) -> list[IndexerRun]:
        """Recent executions of the indexer that feeds this index (newest first); [] if unknown."""
        ...

    def close(self) -> None: ...


class HttpIndexPort:
    def __init__(
        self,
        endpoint: str,
        *,
        index: str = INDEX_B,
        client: httpx.Client | None = None,
        token_source: Callable[[], str] = lambda: get_app_token(SEARCH_SCOPE),
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._endpoint = endpoint.rstrip("/")
        self._index = index
        self._http = client or httpx.Client(timeout=60.0)
        self._owns_client = client is None
        self._token_source = token_source
        self._clock = clock
        self._lock = threading.Lock()
        self._bearer: tuple[str, float] | None = None
        self._acl_cache: tuple[float, dict[str, IndexAcl]] | None = None

    def _service_token(self) -> str:
        with self._lock:
            if self._bearer is None or self._bearer[1] - 300 < self._clock():
                token = self._token_source()
                exp = describe_token(token).get("exp")
                expires = float(exp) if isinstance(exp, int | float) else self._clock() + 600
                self._bearer = (token, expires)
            return self._bearer[0]

    def _search(self, body: dict[str, Any]) -> dict[str, Any]:
        try:
            resp = self._http.post(
                f"{self._endpoint}/indexes/{self._index}/docs/search",
                params={"api-version": API},
                json=body,
                headers={"Authorization": f"Bearer {self._service_token()}"},
            )
        except httpx.TransportError as exc:
            raise IndexUnavailable(f"transport:{type(exc).__name__}") from None
        except Exception as exc:  # the token source failed
            raise IndexUnavailable(f"service-token:{type(exc).__name__}") from None
        if resp.status_code != 200:
            raise IndexUnavailable(f"http:{resp.status_code}")
        try:
            data = resp.json()
        except ValueError:
            raise IndexUnavailable("invalid-json") from None
        if not isinstance(data, dict) or not isinstance(data.get("value"), list):
            raise IndexUnavailable("unexpected-shape")
        return data

    def query(self, text: str, top: int, skip: int = 0) -> Fetched:
        data = self._search(
            {
                "search": text,
                "select": "id,metadata_spo_item_name,content",
                "top": max(1, min(top, MAX_TOP)),
                "skip": max(0, skip),
                "count": True,
            }
        )
        hits: list[Hit] = []
        unmapped = 0
        for raw in data["value"]:
            try:
                doc_id = item_id_of_key(str(raw["id"]))
            except (KeyError, ValueError):
                unmapped += 1
                continue
            hits.append(
                Hit(
                    doc_id,
                    str(raw.get("metadata_spo_item_name") or ""),
                    str(raw.get("content") or ""),
                )
            )
        count = data.get("@odata.count")
        return Fetched(hits, unmapped, count if isinstance(count, int) else None)

    def stored_acl(self, doc_id: str) -> IndexAcl | None:
        with self._lock:
            cached = self._acl_cache
        if cached is None or self._clock() - cached[0] > ACL_CACHE_S:
            data = self._search({"search": "*", "select": "id,UserIds,GroupIds", "top": MAX_TOP})
            table: dict[str, IndexAcl] = {}
            for raw in data["value"]:
                try:
                    table[item_id_of_key(str(raw["id"]))] = IndexAcl(
                        [str(v) for v in raw.get("UserIds") or []],
                        [str(v) for v in raw.get("GroupIds") or []],
                    )
                except (KeyError, ValueError):
                    continue
            cached = (self._clock(), table)
            with self._lock:
                self._acl_cache = cached
        return cached[1].get(doc_id)

    def indexer_runs(self) -> list[IndexerRun]:
        try:
            resp = self._http.get(
                f"{self._endpoint}/indexers/{INDEXER_B}/status",
                params={"api-version": API},
                headers={"Authorization": f"Bearer {self._service_token()}"},
            )
            if resp.status_code != 200:
                return []
            data = resp.json()
        except (httpx.HTTPError, ValueError, RuntimeError):
            return []
        raw = list(data.get("executionHistory") or [])
        last = data.get("lastResult")
        if isinstance(last, dict) and not any(
            r.get("startTime") == last.get("startTime") for r in raw
        ):
            raw.insert(0, last)
        return [parse_run(r) for r in raw if isinstance(r, dict)]

    def close(self) -> None:
        if self._owns_client:
            self._http.close()
