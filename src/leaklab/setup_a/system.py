"""setup_a_basic: Azure AI Search with the SharePoint indexer's ACL ingestion, as a SystemUnderTest.

read(persona, canary) asks the service what that persona's token can see: an exact-phrase search for
the canary, the driver app's bearer for the service and the persona's own token in
x-ms-query-source-authorization. served = the canary string is in a returned document's content.

Only the CURRENT document counts. The query returns each hit's index key, which encodes the Graph
item id (setup_a/keys.py); when the system is given the current item id of every canary (from the
generator's tenant) a hit counts only if its item id IS that id. A copy of the canary text left
behind in the index by an earlier tenant generation is "stale": it is never served, never counts as
evidence that the document is indexed, and is reported once through drain_events
("stale_copy_seen"). Without item ids (older callers, unit tests) the check is off.

Not-indexed is not denied. A trimmed answer is empty for BOTH "the persona may not read it" and "the
document is not in the index", and probes/search_acl_probe.py (stage lookup, 2026-10-17) showed that
a lookup by key is trimmed the same way, even with the admin key. So presence is decided from
evidence the ACL filter cannot hide, and an empty answer is only a denial once the document is known
to be there:
  1. some persona was already served the canary through this system (it is in the index), or
  2. the indexer completed (no failed items, no error naming the document) a run that STARTED after
     the document was created, so that run saw the document.
Otherwise the read is served=None with error "not_indexed" (or "presence_unknown" when the creation
time of the document is not known). It is never served=False. Limit: evidence 2 says the run saw the
document, not that the index holds it; a document the indexer silently skipped would be judged
present and therefore "denied".

The system also reports indexer runs (start, end, status, counts) through drain_events(); the engine
journals them so a report can relate a denial to the run that caused it.
"""

import threading
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from leaklab.runner.model import Observation, RunnerError
from leaklab.runner.setup0 import TokenCache
from leaklab.setup_a.client import SearchPort
from leaklab.setup_a.keys import item_id_of_key

SYSTEM_NAME = "setup_a_basic"
TOKEN_MAX_AGE_S = 120.0  # Entra throttles repeated password sign-ins (AADSTS50196, verified)
RUNS_POLL_S = 15.0


class SetupABasic:
    name = SYSTEM_NAME

    def __init__(
        self,
        *,
        search: SearchPort,
        upns: Mapping[str, str],
        tokens: TokenCache,
        created_at: Mapping[str, float] | None = None,
        keys: Mapping[str, str] | None = None,
        item_ids: Mapping[str, str] | None = None,
        clock: Callable[[], float] = time.time,
        runs_poll_s: float = RUNS_POLL_S,
    ) -> None:
        self._search = search
        self._upns = dict(upns)
        self._tokens = tokens
        self._created = dict(created_at or {})  # canary -> epoch time the document was created
        self._keys = dict(keys or {})  # canary -> index key (to match errors in a run)
        # canary -> the Graph item id of its CURRENT document; None switches the check off
        self._item_ids = None if item_ids is None else dict(item_ids)
        self._stale_seen: set[tuple[str, str]] = set()
        self._stale_events: list[dict[str, Any]] = []
        self._clock = clock
        self._runs_poll_s = runs_poll_s
        self._lock = threading.Lock()
        self._seen: set[str] = set()  # canaries known to be in the index
        self._runs: list[Any] = []
        self._runs_at = float("-inf")
        self._emitted_start: set[float | None] = set()
        self._emitted_done: set[float | None] = set()

    # -- SystemUnderTest ---------------------------------------------------------------------

    def prepare(self, personas: Sequence[str], canaries: Sequence[str]) -> None:
        """Sign every persona in and make sure the service answers (an error here, before any
        mutation, beats thousands of error reads later)."""
        for persona in personas:
            self._tokens.get(self._upn(persona))
        if personas and canaries:
            token = self._tokens.get(self._upn(personas[0]))
            status, answer = self._search.query(token, f'"{canaries[0]}"')
            if status != 200:
                raise RunnerError(f"the search service answered HTTP {status}: {answer!r}")
        self._refresh_runs(force=True)

    def read(self, persona: str, canary: str) -> Observation:
        started = self._clock()

        def obs(served: bool | None, **kw: int | str | None) -> Observation:
            return Observation(
                system=self.name,
                persona=persona,
                canary=canary,
                ts=started,
                served=served,
                latency_ms=round((self._clock() - started) * 1000.0, 1),
                **kw,
            )

        try:
            upn = self._upn(persona)
            token = self._tokens.get(upn)
        except Exception as exc:
            return obs(None, error=f"token:{type(exc).__name__}")
        status, answer = self._search.query(token, f'"{canary}"')
        if status == 401:
            self._tokens.invalidate(upn)
        if status != 200 or not isinstance(answer, list):
            return obs(
                None, status=status or None, error=f"http:{status}" if status else str(answer)
            )
        hits = [doc for doc in answer if canary in (doc.get("content") or "")]
        current = [doc for doc in hits if self._is_current(canary, doc)]
        if current:
            with self._lock:
                self._seen.add(canary)
            return obs(True, status=200)
        present = self._present(canary)
        if present is True:
            return obs(False, status=200)  # the document is there and this persona is not shown it
        return obs(
            None, status=200, error="not_indexed" if present is False else "presence_unknown"
        )

    def _is_current(self, canary: str, doc: Mapping[str, Any]) -> bool:
        """True if the hit is the canary's current document; a stale copy is noted and refused."""
        if self._item_ids is None or canary not in self._item_ids:
            return True
        try:
            item = item_id_of_key(str(doc["id"]))
        except (KeyError, ValueError):
            item = ""
        if item == self._item_ids[canary]:
            return True
        with self._lock:
            if (canary, item) not in self._stale_seen:
                self._stale_seen.add((canary, item))
                self._stale_events.append(
                    {"kind": "stale_copy_seen", "canary": canary, "item_id": item or None}
                )
        return False

    def drain_events(self) -> list[dict[str, Any]]:
        """New indexer events since the last call (started / finished runs), as plain dicts."""
        runs = self._refresh_runs()
        with self._lock:
            events: list[dict[str, Any]] = self._stale_events
            self._stale_events = []
        for run in sorted(runs, key=lambda r: r.start or 0.0):
            if run.start not in self._emitted_start:
                self._emitted_start.add(run.start)
                events.append({"kind": "indexer_run_started", "start": run.start})
            if run.finished and run.start not in self._emitted_done:
                self._emitted_done.add(run.start)
                events.append(
                    {
                        "kind": "indexer_run",
                        "start": run.start,
                        "end": run.end,
                        "status": run.status,
                        "items_processed": run.items_processed,
                        "items_failed": run.items_failed,
                        "error": run.error,
                    }
                )
        return events

    def close(self) -> None:
        close = getattr(self._search, "close", None)
        if callable(close):
            close()

    # -- presence ------------------------------------------------------------------------------

    def _present(self, canary: str) -> bool | None:
        with self._lock:
            if canary in self._seen:
                return True
        created = self._created.get(canary)
        if created is None:
            return None
        key = self._keys.get(canary)
        runs = self._refresh_runs()
        if key is not None and any(key in run.error_keys for run in runs):
            return False
        good = [
            r
            for r in runs
            if r.finished
            and r.status == "success"
            and r.items_failed == 0
            and r.start is not None
            and r.start >= created
        ]
        return bool(good)

    def _refresh_runs(self, force: bool = False) -> list[Any]:
        with self._lock:
            if force or self._clock() - self._runs_at >= self._runs_poll_s:
                self._runs = self._search.indexer_runs()
                self._runs_at = self._clock()
            return list(self._runs)

    def _upn(self, persona: str) -> str:
        try:
            return self._upns[persona]
        except KeyError:
            raise RunnerError(f"no UPN known for persona {persona!r}") from None
