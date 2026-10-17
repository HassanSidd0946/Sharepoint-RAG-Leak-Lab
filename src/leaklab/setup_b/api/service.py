"""The service core (no web framework): validate the token, search the unfiltered index with the
service identity, over-fetch k*5, decide every candidate from the authoritative state, log, return.

Flow of query(token, text, k):
  1. validate the token (signature, tenant, audience): InvalidToken otherwise, nothing else happens;
  2. one freshness reading for the whole request (so every decision in it uses the same one);
  3. fetch k*OVER_FETCH hits from ll-sp-index-b (service identity; unfiltered);
  4. decide EVERY fetched hit with StateReader.decide_with (acl_store x membership_store only; the
     index's own ACL fields are never read here). A hit whose key cannot be mapped is dropped;
  5. append all decisions + one summary record to the hash-chained log. If the log cannot be written
     the request FAILS (no log, no answer): nothing is returned that was not recorded;
  6. return the first k allowed hits and the numbers (fetched, allowed, returned, shortfall).

Fail closed: stale membership = no group-based grant is honored, stale ACL = nothing is (see
setup_b/state/access.py); an unknown principal is denied; any error is an exception, never a
partial or allow-by-default answer. The query text is never logged, only its sha256.
"""

import hashlib
import threading
import uuid
from collections import Counter, deque
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

from leaklab.setup_b.api.decisionlog import DecisionLog, Entry
from leaklab.setup_b.api.search import MAX_TOP, IndexPort
from leaklab.setup_b.api.tokens import Principal, TokenValidator
from leaklab.setup_b.state import acl, membership
from leaklab.setup_b.state.access import Decision, Freshness, StateReader
from leaklab.setup_b.state.model import Edge
from leaklab.setup_b.state.repository import StateRepository

OVER_FETCH = 5
MAX_K = 50
MAX_QUERY_CHARS = 1000


class LogUnavailable(Exception):
    """The decision log could not be written: the request must fail."""


@dataclass(frozen=True)
class ResultDoc:
    doc_id: str
    name: str
    content: str


@dataclass(frozen=True)
class QueryStats:
    k: int
    fetch_limit: int
    fetched: int  # hits the index returned for the over-fetched window
    unmapped: int  # dropped: key not mappable to a drive item id
    allowed: int  # of the fetched hits, how many the state allows
    returned: int  # min(k, allowed)
    degraded: bool  # the membership state was stale: group grants were not honored
    denied: dict[str, int] = field(default_factory=dict)  # reason -> count
    allowed_total: int | None = None  # only with measure_recall: allowed among ALL matches (<=1000)

    @property
    def shortfall(self) -> int:
        """How many fewer than k came back (0 if k results came back)."""
        return self.k - self.returned


@dataclass(frozen=True)
class QueryOutcome:
    request_id: str
    principal: Principal
    results: list[ResultDoc]
    stats: QueryStats


class QueryService:
    def __init__(
        self,
        *,
        reader: StateReader,
        repo: StateRepository,
        index: IndexPort,
        log: DecisionLog,
        validator: TokenValidator,
        clock: Callable[[], float],
        over_fetch: int = OVER_FETCH,
    ) -> None:
        if over_fetch < 1:
            raise ValueError("over_fetch must be at least 1")
        self._reader = reader
        self._repo = repo
        self._index = index
        self._log = log
        self._validator = validator
        self._clock = clock
        self._over_fetch = over_fetch
        self._lock = threading.Lock()
        self._counters: Counter[str] = Counter()
        self._recent: deque[tuple[int, int, int]] = deque(maxlen=200)  # (k, fetched, returned)

    # -- /query ----------------------------------------------------------------------------------

    def query(self, token: str, text: str, k: int, *, measure_recall: bool = False) -> QueryOutcome:
        if not 1 <= k <= MAX_K or not text.strip() or len(text) > MAX_QUERY_CHARS:
            raise ValueError("k must be 1..50 and the query 1..1000 characters")
        request_id = uuid.uuid4().hex
        principal = self._validator.validate(token)  # InvalidToken propagates
        fresh = self._reader.freshness()
        limit = min(k * self._over_fetch, MAX_TOP)
        fetched = self._index.query(text, limit)  # IndexUnavailable propagates
        decisions = [
            (h, self._reader.decide_with(principal.oid, h.doc_id, fresh)) for h in fetched.hits
        ]
        allowed = [h for h, d in decisions if d.allowed]
        allowed_total: int | None = None
        if measure_recall:
            everything = self._index.query(text, MAX_TOP)
            allowed_total = sum(
                1
                for h in everything.hits
                if self._reader.decide_with(principal.oid, h.doc_id, fresh).allowed
            )
        denied = Counter(d.reason for _, d in decisions if not d.allowed)
        shown = allowed[:k]
        stats = QueryStats(
            k=k,
            fetch_limit=limit,
            fetched=len(fetched.hits),
            unmapped=fetched.unmapped,
            allowed=len(allowed),
            returned=len(shown),
            degraded=not fresh.membership_fresh or not fresh.acl_fresh,
            denied=dict(denied),
            allowed_total=allowed_total,
        )
        shown_ids = {h.doc_id for h in shown}
        now = self._clock()
        entries = [
            Entry(
                now,
                "decision",
                principal.oid,
                h.doc_id,
                d.allowed,
                d.reason,
                request_id,
                {"via": d.via, "returned": h.doc_id in shown_ids},
            )
            for h, d in decisions
        ]
        entries.append(
            Entry(
                now,
                "query",
                principal.oid,
                "",
                bool(shown),
                "degraded" if stats.degraded else ("ok" if shown else "no_results"),
                request_id,
                {
                    "query_sha256": hashlib.sha256(text.encode()).hexdigest(),
                    **{k_: v for k_, v in asdict(stats).items() if k_ != "denied"},
                    "denied": stats.denied,
                    "membership_age_s": fresh.membership_age_s,
                    "acl_age_s": fresh.acl_age_s,
                },
            )
        )
        try:
            self._log.append(entries)
        except Exception:
            self._count("log_errors")
            raise LogUnavailable("the decision log could not be written") from None
        self._record(stats, decisions)
        return QueryOutcome(
            request_id, principal, [ResultDoc(h.doc_id, h.name, h.content) for h in shown], stats
        )

    # -- /explain ----------------------------------------------------------------------------------

    def explain(self, user: str, doc_id: str) -> dict[str, Any]:
        """Why the state allows or denies `user` (object id or UPN) on `doc_id`, step by step."""
        directory = self._repo.load_directory()
        by_upn = {upn: uid for uid, upn in directory.items()}
        user_id = by_upn.get(user.lower(), user.lower())
        upn = self._repo.upn_of(user_id)
        fresh = self._reader.freshness()
        decision = self._reader.decide_with(user_id, doc_id, fresh)
        scope = self._repo.scope_of_item(doc_id)
        grants = self._repo.grants_of_scope(scope) if scope is not None else ()
        groups = self._repo.groups_of(user_id)
        edges = self._repo.load_edges()
        snapshot = self._repo.load_acl()
        scope_item = snapshot.items.get(scope) if scope else None
        out: dict[str, Any] = {
            "user": {"id": user_id, "upn": upn, "groups": sorted(groups)},
            "doc": {"id": doc_id, "known": scope is not None},
            "decision": asdict(decision),
            "freshness": asdict(fresh) | {"fresh": fresh.fresh},
            "scope": {
                "id": scope,
                "name": scope_item.name if scope_item else ("site root" if scope else None),
                "own_permissions": bool(scope_item.unique) if scope_item else scope == "root",
            },
            "grants": [
                {
                    "kind": g.kind,
                    "ref": g.ref,
                    "role": g.role,
                    "via": g.via,
                    "confers_read": g.grants_read,
                    "matches_user": _grant_matches(g.kind, g.ref, g.grants_read, upn, groups),
                    "path": _group_path(user_id, g.ref, edges) if g.kind == "entra_group" else None,
                }
                for g in grants
            ],
            "path": self._path_text(decision, user_id, upn, edges),
        }
        stored = self._index_acl_for_comparison(doc_id)
        out["index_acl_snapshot"] = stored
        self._try_log(
            Entry(
                self._clock(),
                "explain",
                user_id,
                doc_id,
                decision.allowed,
                decision.reason,
                uuid.uuid4().hex,
                {"via": decision.via},
            )
        )
        return out

    def _path_text(
        self, decision: Decision, user_id: str, upn: str | None, edges: set[Edge]
    ) -> str:
        if decision.via is None:
            return f"DENY ({decision.reason})"
        kind, _, ref = decision.via.partition(":")
        if kind == "user":
            return f"ALLOW: a role assignment names {ref} directly"
        chain = _group_path(user_id, ref, edges) or [ref]
        return (
            "ALLOW: "
            + " -> ".join([upn or user_id, *chain])
            + " (the group has a read role on the scope)"
        )

    def _index_acl_for_comparison(self, doc_id: str) -> dict[str, Any] | None:
        """What the index stored for the document. COMPARISON ONLY: no decision reads this."""
        try:
            stored = self._index.stored_acl(doc_id)
        except Exception:
            return {"error": "index unavailable"}
        if stored is None:
            return None
        return {
            "user_ids": stored.user_ids,
            "group_ids": stored.group_ids,
            "note": "stale indexer snapshot; shown for comparison, never used for decisions",
        }

    # -- /health and /metrics --------------------------------------------------------------------

    def health(self) -> dict[str, Any]:
        fresh = self._reader.freshness()
        reasons = []
        if not fresh.membership_fresh:
            reasons.append("membership state stale: group-based grants are denied")
        if not fresh.acl_fresh:
            reasons.append("ACL state stale: every grant is denied")
        return {
            "status": "ok" if not reasons else "degraded",
            "reasons": reasons,
            "membership_age_s": fresh.membership_age_s,
            "acl_age_s": fresh.acl_age_s,
            "max_age_s": fresh.max_age_s,
            "poller_errors": {
                name: (state.last_error if (state := self._repo.get_state(name)) else None)
                for name in (membership.NAME, acl.NAME)
            },
        }

    def metrics(self) -> dict[str, Any]:
        fresh: Freshness = self._reader.freshness()
        with self._lock:
            counters = dict(self._counters)
            recent = list(self._recent)
        log_head = self._log.records(1)
        return {
            "poller_lag_s": {"membership": fresh.membership_age_s, "acl": fresh.acl_age_s},
            "max_age_s": fresh.max_age_s,
            "degraded": not fresh.fresh,
            "counters": counters,
            "recent_queries": {
                "n": len(recent),
                "mean_returned": (sum(r[2] for r in recent) / len(recent)) if recent else None,
                "mean_fetched": (sum(r[1] for r in recent) / len(recent)) if recent else None,
                "short_of_k": sum(1 for k, _, ret in recent if ret < k),
            },
            "decision_log": {"last_seq": log_head[-1].seq if log_head else 0},
        }

    def count(self, key: str) -> None:
        self._count(key)

    def close(self) -> None:
        """Release the index client and the decision log connection."""
        self._index.close()
        self._log.close()

    # -- internals -------------------------------------------------------------------------------

    def _count(self, key: str, n: int = 1) -> None:
        with self._lock:
            self._counters[key] += n

    def _record(self, stats: QueryStats, decisions: list[tuple[Any, Decision]]) -> None:
        with self._lock:
            self._counters["queries"] += 1
            if stats.degraded:
                self._counters["queries_degraded"] += 1
            for _, d in decisions:
                self._counters["decisions_allow" if d.allowed else "decisions_deny"] += 1
                self._counters[f"reason:{d.reason}"] += 1
            self._recent.append((stats.k, stats.fetched, stats.returned))

    def _try_log(self, entry: Entry) -> None:
        try:
            self._log.append([entry])
        except Exception:
            self._count("log_errors")
            raise LogUnavailable("the decision log could not be written") from None


def _grant_matches(
    kind: str, ref: str, confers_read: bool, upn: str | None, groups: set[str]
) -> bool:
    if not confers_read:
        return False
    return (kind == "user" and upn is not None and ref == upn) or (
        kind == "entra_group" and ref in groups
    )


def _group_path(user_id: str, target: str, edges: set[Edge]) -> list[str] | None:
    """Shortest chain of groups from the user to `target` (user in A, A in B, ...), or None."""
    parents: dict[str, list[str]] = {}
    for e in edges:
        parents.setdefault(e.member_id, []).append(e.group_id)
    queue: deque[list[str]] = deque([[g] for g in parents.get(user_id, [])])
    seen: set[str] = set()
    while queue:
        path = queue.popleft()
        if path[-1] == target:
            return path
        if path[-1] in seen:
            continue
        seen.add(path[-1])
        queue.extend([*path, g] for g in parents.get(path[-1], []))
    return None
