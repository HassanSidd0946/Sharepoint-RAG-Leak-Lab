"""The Azure AI Search data plane as Setup A needs it, behind a small port so tests need no service.

SearchPort has two operations:
  query(user_token, phrase)  an exact-phrase search with the DRIVER app's bearer and the end user's
                             token in x-ms-query-source-authorization (verified 2026-10-17); returns
                             (HTTP status, documents or an error text)
  indexer_runs()             the indexer's recent executions from its status (newest first)

The real HttpSearchPort never raises for operational problems: a transport error is status 0.
Nothing here decides who may read what: the service trims by the user's token, and the system
(system.py) only reports what came back.
"""

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

import httpx

from leaklab.auth import describe_token, get_app_token

API = "2026-08-01-preview"
SEARCH_SCOPE = "https://search.azure.com/.default"
INDEX = "ll-sp-index"
INDEXER = "ll-sp-indexer"
DATASOURCE = "ll-sp-datasource"


@dataclass(frozen=True)
class IndexerRun:
    """One indexer execution, as the status endpoint reports it."""

    start: float | None  # epoch seconds
    end: float | None  # None while it is still running
    status: str  # inProgress | success | transientFailure | persistentFailure | reset ...
    items_processed: int
    items_failed: int
    error: str | None = None
    error_keys: tuple[str, ...] = ()  # document keys the run reported errors for

    @property
    def finished(self) -> bool:
        return self.end is not None and self.status != "inProgress"


class SearchPort(Protocol):
    def query(self, user_token: str, phrase: str) -> tuple[int, list[dict[str, Any]] | str]: ...

    def indexer_runs(self) -> list[IndexerRun]: ...


def parse_time(value: str | None) -> float | None:
    """An ISO-8601 time from the service ("2026-10-17T08:28:19.056Z") as epoch seconds."""
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def parse_run(raw: dict[str, Any]) -> IndexerRun:
    errors = raw.get("errors") or []
    return IndexerRun(
        start=parse_time(raw.get("startTime")),
        end=parse_time(raw.get("endTime")),
        status=str(raw.get("status", "")),
        items_processed=int(raw.get("itemsProcessed") or 0),
        items_failed=int(raw.get("itemsFailed") or 0),
        error=raw.get("errorMessage"),
        error_keys=tuple(str(e["key"]) for e in errors if isinstance(e, dict) and e.get("key")),
    )


class HttpSearchPort:
    """The real service. The bearer is the driver app's app-only token for the search scope."""

    def __init__(
        self,
        endpoint: str,
        *,
        client: httpx.Client | None = None,
        token_source: Callable[[], str] = lambda: get_app_token(SEARCH_SCOPE),
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._endpoint = endpoint.rstrip("/")
        self._http = client or httpx.Client(timeout=60.0)
        self._owns_client = client is None
        self._token_source = token_source
        self._clock = clock
        self._bearer: tuple[str, float] | None = None
        self._lock = threading.Lock()

    def _driver_token(self) -> str:
        with self._lock:
            if self._bearer is None or self._bearer[1] - 300 < self._clock():
                token = self._token_source()
                exp = describe_token(token).get("exp")
                expires = float(exp) if isinstance(exp, int | float) else self._clock() + 600
                self._bearer = (token, expires)
            return self._bearer[0]

    def query(self, user_token: str, phrase: str) -> tuple[int, list[dict[str, Any]] | str]:
        body = {"search": phrase, "select": "id,metadata_spo_item_name,content", "top": 5}
        try:
            resp = self._http.post(
                f"{self._endpoint}/indexes/{INDEX}/docs/search",
                params={"api-version": API},
                json=body,
                headers={
                    "Authorization": f"Bearer {self._driver_token()}",
                    "x-ms-query-source-authorization": user_token,
                },
            )
        except httpx.TransportError as exc:
            return 0, f"transport:{type(exc).__name__}"
        except Exception as exc:  # token source failed
            return 0, f"driver-token:{type(exc).__name__}"
        if resp.status_code != 200:
            return resp.status_code, resp.text[:300]
        try:
            return 200, list(resp.json().get("value", []))
        except ValueError:
            return 200, "invalid-json"

    def indexer_runs(self) -> list[IndexerRun]:
        try:
            resp = self._http.get(
                f"{self._endpoint}/indexers/{INDEXER}/status",
                params={"api-version": API},
                headers={"Authorization": f"Bearer {self._driver_token()}"},
            )
            if resp.status_code != 200:
                return []
            data = resp.json()
        except (httpx.HTTPError, ValueError):
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
