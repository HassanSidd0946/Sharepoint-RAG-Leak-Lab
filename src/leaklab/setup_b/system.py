"""setup_b: our API in front of the unfiltered index, as a SystemUnderTest.

read(persona, canary) does what a client does: POST /query with the persona's own token and an
exact-phrase query for the canary. served = the canary string is in a returned document's content.

Setup B queries a SEPARATE, UNFILTERED index (ll-sp-index-b) over the same SharePoint data source;
Setup A uses the ACL-filtered index. This adapter only talks HTTP to the API: it holds no service
token, no admin key and no access to the state; the API decides.

Not-indexed is not denied. The adapter asks for the service's debug stats (the server of a benchmark
run enables them), which include the item ids of the hits the index returned BEFORE the filter. If
the canary's CURRENT document (its item id, from the generator's tenant) is not among them, the
document is not in the index yet (a stale copy of the same text from an earlier tenant generation
does not count), so the read is served=None with error "not_indexed", never a denial. A document
that IS in the index and was removed by the filter is a real denial (served=False). Without item
ids the adapter falls back to "the phrase matched nothing at all" (fetched == 0).

Readiness: prepare() waits for GET /ready (two quiet polls per poller and one completed
verification, setup_b/state/readiness.py), at most `ready_cap_s` (5 minutes), BEFORE the
baseline, and reports `setup_b_ready` with the time it took (from the start of the build,
`ready_started_at`) and `setup_b_startup`: the first sync's numbers against what the state held
at readiness, which shows
whether the first sync was incomplete. Reads start only after prepare, so a lockout that is just
start-up is not counted; if the cap passes the run goes on and the event says ready=false.

Journal events (drain_events):
  system_health      the API's /health reading whenever its status changes (ok/degraded, ages)
  setup_b_ready      ready (bool), time_to_ready_s, the readiness state, the cap
  setup_b_startup    first sync vs state at readiness (counts, difference, who found the rest)
  poller_lag         the membership and ACL pollers' age (seconds since their last success), every
                     `lag_every_s`: the lag of Setup B's own state, for the benchmark report
  indexer_run(_started)  runs of the indexer that feeds ll-sp-index-b (same fields as Setup A's)
  postfilter_recall  the recall cost of post-filtering: for broad queries ("*") at several k, how
                     many results came back versus k, versus the number of readable documents
                     among ALL matches (a sample round at prepare, then every `recall_every_s`)
"""

import threading
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import httpx

from leaklab.runner.model import Observation, RunnerError
from leaklab.runner.setup0 import TokenCache
from leaklab.setup_a.client import IndexerRun

SYSTEM_NAME = "setup_b"
BROAD_QUERY = "*"
RECALL_KS = (3, 5, 10)
READY_WAIT_S = 120.0
RUNS_POLL_S = 15.0


class SetupB:
    name = SYSTEM_NAME

    def __init__(
        self,
        *,
        base_url: str,
        upns: Mapping[str, str],
        tokens: TokenCache,
        clock: Callable[[], float],
        sleep: Callable[[float], None],
        k: int = 5,
        recall_ks: Sequence[int] = RECALL_KS,
        recall_every_s: float = 300.0,
        item_ids: Mapping[str, str] | None = None,
        lag_every_s: float = 30.0,
        ready_cap_s: float = 300.0,
        ready_started_at: float | None = None,
        startup_report: Callable[[float | None], dict[str, Any]] | None = None,
        indexer_runs: Callable[[], list[IndexerRun]] | None = None,
        http: httpx.Client | None = None,
        closers: Sequence[Callable[[], object]] = (),
    ) -> None:
        self._base = base_url.rstrip("/")
        self._upns = dict(upns)
        self._tokens = tokens
        self._clock = clock
        self._sleep = sleep
        self._k = k
        self._recall_ks = tuple(recall_ks)
        self._recall_every_s = recall_every_s
        self._item_ids = None if item_ids is None else dict(item_ids)  # canary -> current item id
        self._lag_every_s = lag_every_s
        self._ready_cap_s = ready_cap_s
        self._ready_started_at = ready_started_at
        self._startup_report = startup_report
        self._indexer_runs = indexer_runs
        self._lag_at = float("-inf")
        self._runs_at = float("-inf")
        self._emitted_start: set[float | None] = set()
        self._emitted_done: set[float | None] = set()
        self._http = http or httpx.Client(timeout=60.0)
        self._closers = list(closers)
        self._lock = threading.Lock()
        self._pending: list[dict[str, Any]] = []
        self._last_status: str | None = None
        self._recall_at = float("-inf")
        self._personas: list[str] = []

    # -- SystemUnderTest ---------------------------------------------------------------------

    def prepare(self, personas: Sequence[str], canaries: Sequence[str]) -> None:
        """Sign every persona in, wait until the API reports ok, take the first recall sample."""
        self._personas = list(personas)
        for persona in personas:
            self._tokens.get(self._upn(persona))
        deadline = self._clock() + READY_WAIT_S
        while True:
            health = self._health()
            if health is not None and health.get("status") == "ok":
                break
            if self._clock() > deadline:
                raise RunnerError(
                    f"the Setup B API is not healthy after {READY_WAIT_S:.0f} s: {health!r}"
                )
            self._sleep(2.0)
        self._wait_until_ready()
        self._sample_recall()

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
        status, body = self._query(token, f'"{canary}"', self._k, measure_recall=False)
        if status == 401:
            self._tokens.invalidate(upn)
        if status != 200 or body is None:
            return obs(
                None, status=status or None, error=f"http:{status}" if status else "transport"
            )
        current = self._item_ids.get(canary) if self._item_ids else None
        if any(
            canary in str(r.get("content") or "")
            and (current is None or r.get("doc_id") == current)
            for r in body.get("results", [])
        ):
            return obs(True, status=200)
        stats = body.get("stats")
        if not isinstance(stats, dict) or "fetched" not in stats:
            # without the pre-filter count "denied" and "not indexed" cannot be told apart
            return obs(None, status=200, error="no_debug_stats")
        if current is not None and "doc_ids" in stats:
            present = current in stats["doc_ids"]
        else:
            present = int(stats["fetched"]) > 0  # fallback: the phrase matched something
        if not present:
            return obs(None, status=200, error="not_indexed")  # the current document is missing
        return obs(False, status=200)

    def drain_events(self) -> list[dict[str, Any]]:
        health = self._health()
        if health is not None and health.get("status") != self._last_status:
            self._last_status = str(health.get("status"))
            self._push(
                {
                    "kind": "system_health",
                    "t": self._clock(),
                    "status": health.get("status"),
                    "reasons": health.get("reasons"),
                    "membership_age_s": health.get("membership_age_s"),
                    "acl_age_s": health.get("acl_age_s"),
                }
            )
        if health is not None and self._clock() - self._lag_at >= self._lag_every_s:
            self._lag_at = self._clock()
            self._push(
                {
                    "kind": "poller_lag",
                    "t": self._lag_at,
                    "membership_age_s": health.get("membership_age_s"),
                    "acl_age_s": health.get("acl_age_s"),
                }
            )
        self._indexer_events()
        if self._clock() - self._recall_at >= self._recall_every_s:
            self._sample_recall()
        with self._lock:
            events, self._pending = self._pending, []
        return events

    def close(self) -> None:
        self._http.close()
        for closer in reversed(self._closers):
            try:
                closer()
            except Exception:  # shutting down must not hide the run's result
                pass

    # -- internals ------------------------------------------------------------------------------

    def _query(
        self, token: str, text: str, k: int, *, measure_recall: bool
    ) -> tuple[int, dict[str, Any] | None]:
        try:
            resp = self._http.post(
                f"{self._base}/query",
                json={
                    "persona_token": token,
                    "query": text,
                    "k": k,
                    "debug": True,
                    "measure_recall": measure_recall,
                },
            )
        except httpx.TransportError:
            return 0, None
        if resp.status_code != 200:
            return resp.status_code, None
        try:
            data = resp.json()
        except ValueError:
            return 200, None
        return 200, data if isinstance(data, dict) else None

    def _ready(self) -> dict[str, Any] | None:
        """GET /ready: the body whether the answer is 200 or 503; None if unreachable."""
        try:
            data = self._http.get(f"{self._base}/ready").json()
        except (httpx.HTTPError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def _wait_until_ready(self) -> None:
        """Wait for /ready (at most the cap) and journal how long it took (see the module doc)."""
        waited_from = self._clock()
        deadline = waited_from + self._ready_cap_s
        state = self._ready()
        while not (state and state.get("ready")) and self._clock() < deadline:
            self._sleep(2.0)
            state = self._ready()
        ready = bool(state and state.get("ready"))
        now = self._clock()
        began = self._ready_started_at if self._ready_started_at is not None else waited_from
        self._push(
            {
                "kind": "setup_b_ready",
                "t": now,
                "ready": ready,
                "time_to_ready_s": round(now - began, 3) if ready else None,
                "waited_in_prepare_s": round(now - waited_from, 3),
                "cap_s": self._ready_cap_s,
                "state": state,
            }
        )
        if self._startup_report is not None:
            try:
                report = self._startup_report(now if ready else None)
            except Exception:  # diagnostics must never stop a run
                report = {"error": "startup report failed"}
            self._push({"kind": "setup_b_startup", "t": now, **report})

    def _health(self) -> dict[str, Any] | None:
        try:
            resp = self._http.get(f"{self._base}/health")
            data = resp.json()
        except (httpx.HTTPError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def _sample_recall(self) -> None:
        """Broad queries at several k: what post-filtering costs in results (see the module doc)."""
        self._recall_at = self._clock()
        for persona in self._personas:
            try:
                token = self._tokens.get(self._upn(persona))
            except Exception:
                continue
            for k in self._recall_ks:
                status, body = self._query(token, BROAD_QUERY, k, measure_recall=True)
                stats = (body or {}).get("stats")
                if status != 200 or not isinstance(stats, dict):
                    continue
                total = stats.get("allowed_total")
                returned = int(stats["returned"])
                self._push(
                    {
                        "kind": "postfilter_recall",
                        "t": self._clock(),
                        "persona": persona,
                        "query": BROAD_QUERY,
                        "k": k,
                        "fetch_limit": stats["fetch_limit"],
                        "fetched": stats["fetched"],
                        "allowed_in_window": stats["allowed"],
                        "returned": returned,
                        "allowed_total": total,
                        "shortfall": k - returned,
                        "lost_vs_unlimited": (min(k, int(total)) - returned)
                        if total is not None
                        else 0,
                        "degraded": bool((body or {}).get("degraded")),
                    }
                )

    def _indexer_events(self) -> None:
        """Started / finished runs of the indexer behind ll-sp-index-b, each reported once."""
        if self._indexer_runs is None or self._clock() - self._runs_at < RUNS_POLL_S:
            return
        self._runs_at = self._clock()
        try:
            runs = self._indexer_runs()
        except Exception:  # reporting must never stop the run
            return
        for run in sorted(runs, key=lambda r: r.start or 0.0):
            if run.start not in self._emitted_start:
                self._emitted_start.add(run.start)
                self._push({"kind": "indexer_run_started", "start": run.start})
            if run.finished and run.start not in self._emitted_done:
                self._emitted_done.add(run.start)
                self._push(
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

    def _push(self, event: dict[str, Any]) -> None:
        with self._lock:
            self._pending.append(event)

    def _upn(self, persona: str) -> str:
        try:
            return self._upns[persona]
        except KeyError:
            raise RunnerError(f"no UPN known for persona {persona!r}") from None
