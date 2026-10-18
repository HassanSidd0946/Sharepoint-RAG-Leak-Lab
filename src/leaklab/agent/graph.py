"""The agent as a LangGraph graph, in two modes.

    embed -> cache_lookup -+-> [cache_guard] -> deliver_cached ----------------------+
                           |                                                          |
                           +-> memory_recall -> [memory_guard] -> retrieve ->           |
                               build_contexts -> [context_guard] -> generate ->         |
                               write_back ----------------------------------------------+-> record -> END

NAIVE: the cache and the memory are shared by every user and hold no permission metadata. A cache
hit (nearest entry with similarity >= the threshold) is delivered as it is; recalled memory items go
into the LLM's context as they are. The nodes in [brackets] do not exist.

GUARDED: every cache entry and memory item stores the sources it was made from (drive item ids)
and the tenant generation. The bracketed nodes re-check, for the CURRENT user, EVERY source against
Setup B's state (guard.py): a cache hit that fails the check becomes a miss, a memory item that
fails is dropped, and before the LLM is called every piece of context is checked once more. Any
failure or unknown means deny. A user may use an entry another user created if, and only if, they
may read all of its sources today. Threads (the checkpointer's conversations) belong to the user
who started them; another user's thread is denied (threads.py).

Every lookup, guard decision and retrieval is passed to `journal` as a small dict; the S6 driver
writes them to its journal. The LLM only ever sees what is in `contexts`.
"""

import operator
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Any, Literal, TypedDict

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from leaklab.agent.embedding import Embedder
from leaklab.agent.guard import AccessChecker, check_sources
from leaklab.agent.llm import LLM
from leaklab.agent.retrieval import RetrievalError, Retriever
from leaklab.agent.stores import MemoryStore, SemanticCache
from leaklab.agent.threads import ThreadDenied, ThreadRegistry
from leaklab.agent.types import (
    CacheEntry,
    Context,
    Delivery,
    MemoryItem,
    Passage,
    SourceRef,
    UserCtx,
)

Mode = Literal["naive", "guarded"]


@dataclass
class AgentConfig:
    mode: Mode
    k: int = 3  # passages retrieved per question
    cache_threshold: float = 0.65  # cosine similarity at which the nearest entry is a hit
    memory_threshold: float = 0.30
    memory_top: int = 3
    generation: str = ""  # tenant generation (stored with sources in GUARDED mode)


class AgentState(TypedDict, total=False):
    question: str
    persona: str
    oid: str
    upn: str
    embedding: list[float]
    cache: dict[str, Any]
    cache_entry: dict[str, Any] | None
    memory_items: list[dict[str, Any]]
    memory: list[dict[str, Any]]
    passages: list[dict[str, Any]]
    contexts: list[dict[str, Any]]
    answer: str
    via: str
    guard: Annotated[list[dict[str, Any]], operator.add]
    history: Annotated[list[dict[str, Any]], operator.add]


def _src(sources: tuple[SourceRef, ...] | None) -> list[dict[str, str]] | None:
    return (
        None
        if sources is None
        else [{"item_id": s.item_id, "generation": s.generation} for s in sources]
    )


def _unsrc(raw: list[dict[str, str]] | None) -> tuple[SourceRef, ...] | None:
    return None if raw is None else tuple(SourceRef(s["item_id"], s["generation"]) for s in raw)


def fact_line(passage: Passage) -> str:
    """The memory item of a document: its title, topic and reference code lines, joined."""
    lines = [ln.strip() for ln in passage.content.splitlines() if ln.strip()]
    keep = [lines[0]] if lines else []
    keep += [ln for ln in lines if ln.startswith(("Topic:", "Reference code:"))]
    return " | ".join(keep) if keep else passage.content[:200]


class Agent:
    def __init__(
        self,
        *,
        config: AgentConfig,
        retriever: Retriever,
        llm: LLM,
        embedder: Embedder,
        cache: SemanticCache,
        memory: MemoryStore,
        threads: ThreadRegistry,
        clock: Callable[[], float],
        access: AccessChecker | None = None,
        checkpointer: BaseCheckpointSaver[Any] | None = None,
        journal: Callable[[dict[str, Any]], None] = lambda event: None,
    ) -> None:
        if config.mode == "guarded" and access is None:
            raise ValueError("the GUARDED mode needs an access checker (Setup B's state)")
        self.cfg = config
        self.retriever, self.llm, self.embedder = retriever, llm, embedder
        self.cache, self.memory, self.threads = cache, memory, threads
        self.access = access
        self._clock = clock
        self._journal = journal
        self.graph = self._build(checkpointer or MemorySaver())

    # -- public ------------------------------------------------------------------------------------

    def ask(self, user: UserCtx, question: str, thread_id: str | None = None) -> Delivery:
        thread = thread_id or f"user:{user.oid}:default"
        owns = self.threads.claim(thread, user.oid, self._clock())
        if self.cfg.mode == "guarded" and not owns:
            self._emit("thread_denied", user, thread_id=thread)
            raise ThreadDenied(f"thread {thread!r} belongs to another user")
        state: AgentState = self.graph.invoke(
            {"question": question, "persona": user.persona, "oid": user.oid, "upn": user.upn},
            {"configurable": {"thread_id": thread}},
        )
        return Delivery(
            answer=state["answer"],
            via=state["via"],
            cache=state.get("cache", {}),
            memory=state.get("memory", []),
            passages=[p["item_id"] for p in state.get("passages", [])],
            guard=state.get("guard", []),
            thread_id=thread,
        )

    def history(self, user: UserCtx, thread_id: str) -> list[dict[str, Any]]:
        """The questions and answers of a thread. GUARDED: only its owner may read it."""
        owner = self.threads.owner_of(thread_id)
        if self.cfg.mode == "guarded" and owner is not None and owner != user.oid:
            self._emit("thread_denied", user, thread_id=thread_id)
            raise ThreadDenied(f"thread {thread_id!r} belongs to another user")
        snapshot = self.graph.get_state({"configurable": {"thread_id": thread_id}})
        return list(snapshot.values.get("history", []))

    # -- journal -----------------------------------------------------------------------------------

    def _emit(self, kind: str, user: UserCtx | AgentState, **info: Any) -> None:
        who = user.persona if isinstance(user, UserCtx) else user.get("persona")
        self._journal(
            {"kind": kind, "t": self._clock(), "persona": who, "mode": self.cfg.mode, **info}
        )

    # -- nodes -------------------------------------------------------------------------------------

    def _embed(self, state: AgentState) -> dict[str, Any]:
        return {"embedding": self.embedder.embed(state["question"])}

    def _cache_lookup(self, state: AgentState) -> dict[str, Any]:
        entry, sim = self.cache.lookup(state["embedding"])
        candidate = entry is not None and sim >= self.cfg.cache_threshold
        info = {
            "similarity": round(sim, 4),
            "threshold": self.cfg.cache_threshold,
            "candidate": candidate,
            "entry_id": entry.id if entry else None,
            "entry_owner": entry.owner if entry else None,
            "served": False,
            "denied_by_guard": False,
        }
        self._emit("cache_lookup", state, **info)
        raw = None
        if candidate and entry is not None:
            raw = {"id": entry.id, "answer": entry.answer, "sources": _src(entry.sources)}
        return {"cache": info, "cache_entry": raw}

    def _after_lookup(self, state: AgentState) -> str:
        if not state["cache"]["candidate"]:
            return "memory_recall"
        return "cache_guard" if self.cfg.mode == "guarded" else "deliver_cached"

    def _cache_guard(self, state: AgentState) -> dict[str, Any]:
        assert self.access is not None
        entry = state["cache_entry"]
        assert entry is not None
        verdict = check_sources(self.access, state["oid"], _unsrc(entry["sources"]))
        record = {
            "target": "cache",
            "entry_id": entry["id"],
            "allowed": verdict.allowed,
            "reason": verdict.reason,
            "results": [(r.item_id, r.allowed, r.reason) for r in verdict.results],
        }
        self._emit("cache_guard", state, **record)
        cache = {
            **state["cache"],
            "denied_by_guard": not verdict.allowed,
            "guard_reason": verdict.reason,
        }
        return {
            "cache": cache,
            "cache_entry": entry if verdict.allowed else None,
            "guard": [record],
        }

    def _after_guard(self, state: AgentState) -> str:
        return "deliver_cached" if state.get("cache_entry") else "memory_recall"

    def _deliver_cached(self, state: AgentState) -> dict[str, Any]:
        entry = state["cache_entry"]
        assert entry is not None
        return {
            "answer": entry["answer"],
            "via": "cache",
            "cache": {**state["cache"], "served": True},
            "memory": [],
            "passages": [],
        }

    def _memory_recall(self, state: AgentState) -> dict[str, Any]:
        found = self.memory.search(
            state["embedding"], self.cfg.memory_top, self.cfg.memory_threshold
        )
        items = [
            {"id": it.id, "text": it.text, "sources": _src(it.sources), "similarity": round(sim, 4)}
            for it, sim in found
        ]
        info = [
            {"id": i["id"], "similarity": i["similarity"], "used": True, "denied_by_guard": False}
            for i in items
        ]
        self._emit("memory_recall", state, items=info, threshold=self.cfg.memory_threshold)
        return {"memory_items": items, "memory": info}

    def _memory_guard(self, state: AgentState) -> dict[str, Any]:
        assert self.access is not None
        kept: list[dict[str, Any]] = []
        info: list[dict[str, Any]] = []
        records: list[dict[str, Any]] = []
        for item, meta in zip(state["memory_items"], state["memory"], strict=True):
            verdict = check_sources(self.access, state["oid"], _unsrc(item["sources"]))
            records.append(
                {
                    "target": "memory",
                    "item_id": item["id"],
                    "allowed": verdict.allowed,
                    "reason": verdict.reason,
                }
            )
            info.append({**meta, "used": verdict.allowed, "denied_by_guard": not verdict.allowed})
            if verdict.allowed:
                kept.append(item)
        for record in records:
            self._emit("memory_guard", state, **record)
        return {"memory_items": kept, "memory": info, "guard": records}

    def _retrieve(self, state: AgentState) -> dict[str, Any]:
        user = UserCtx(state["persona"], state["oid"], state["upn"])
        try:
            passages = self.retriever.retrieve(user, state["question"], self.cfg.k)
        except RetrievalError as exc:
            self._emit("retrieval_error", state, error=str(exc))
            passages = []
        self._emit(
            "retrieval", state, retriever=self.retriever.name, items=[p.item_id for p in passages]
        )
        return {
            "passages": [
                {"item_id": p.item_id, "name": p.name, "content": p.content} for p in passages
            ]
        }

    def _build_contexts(self, state: AgentState) -> dict[str, Any]:
        guarded = self.cfg.mode == "guarded"
        gen = self.cfg.generation
        contexts = [
            {"kind": "memory", "text": i["text"], "sources": i["sources"]}
            for i in state["memory_items"]
        ]
        contexts += [
            {
                "kind": "retrieved",
                "text": p["content"],
                "sources": _src((SourceRef(p["item_id"], gen),)) if guarded else None,
                "item_id": p["item_id"],
                "name": p["name"],
            }
            for p in state["passages"]
        ]
        return {"contexts": contexts}

    def _context_guard(self, state: AgentState) -> dict[str, Any]:
        """Before EVERY LLM call: every piece of context is checked once more, for this user."""
        assert self.access is not None
        kept: list[dict[str, Any]] = []
        records: list[dict[str, Any]] = []
        for ctx in state["contexts"]:
            verdict = check_sources(self.access, state["oid"], _unsrc(ctx["sources"]))
            if verdict.allowed:
                kept.append(ctx)
            else:
                records.append(
                    {
                        "target": "context",
                        "kind": ctx["kind"],
                        "allowed": False,
                        "reason": verdict.reason,
                    }
                )
        self._emit("context_guard", state, checked=len(state["contexts"]), dropped=len(records))
        return {"contexts": kept, "guard": records}

    def _generate(self, state: AgentState) -> dict[str, Any]:
        contexts = [Context(c["kind"], c["text"], _unsrc(c["sources"])) for c in state["contexts"]]
        return {"answer": self.llm.answer(state["question"], contexts), "via": "llm"}

    def _write_back(self, state: AgentState) -> dict[str, Any]:
        contexts = state["contexts"]
        if not contexts:  # a "nothing found" answer is not cached or remembered
            return {}
        guarded = self.cfg.mode == "guarded"
        now = self._clock()
        sources: tuple[SourceRef, ...] | None = None
        if guarded:
            seen: dict[tuple[str, str], SourceRef] = {}
            for c in contexts:
                for s in _unsrc(c["sources"]) or ():
                    seen[(s.item_id, s.generation)] = s
            sources = tuple(seen.values())
        entry = CacheEntry(
            question=state["question"],
            embedding=state["embedding"],
            answer=state["answer"],
            sources=sources,
            owner=state["oid"] if guarded else None,
            created=now,
        )
        cache_id = self.cache.store(entry)
        memory_ids = []
        for c in contexts:
            if c["kind"] != "retrieved":
                continue
            passage = Passage(c["item_id"], c["name"], c["text"])
            text = fact_line(passage)
            item = MemoryItem(
                text=text,
                embedding=self.embedder.embed(text),
                sources=_unsrc(c["sources"]) if guarded else None,
                owner=state["oid"] if guarded else None,
                created=now,
            )
            memory_ids.append(self.memory.add(item))
        self._emit(
            "write_back",
            state,
            cache_id=cache_id,
            memory_ids=memory_ids,
            sources=len(sources or ()),
        )
        return {}

    def _record(self, state: AgentState) -> dict[str, Any]:
        return {
            "history": [
                {"question": state["question"], "answer": state["answer"], "via": state["via"]}
            ]
        }

    # -- assembly ----------------------------------------------------------------------------------

    def _build(self, checkpointer: BaseCheckpointSaver[Any]) -> Any:
        guarded = self.cfg.mode == "guarded"
        g: StateGraph[AgentState] = StateGraph(AgentState)
        for name, fn in (
            ("embed", self._embed),
            ("cache_lookup", self._cache_lookup),
            ("deliver_cached", self._deliver_cached),
            ("memory_recall", self._memory_recall),
            ("retrieve", self._retrieve),
            ("build_contexts", self._build_contexts),
            ("generate", self._generate),
            ("write_back", self._write_back),
            ("record", self._record),
        ):
            g.add_node(name, fn)
        g.add_edge(START, "embed")
        g.add_edge("embed", "cache_lookup")
        if guarded:
            g.add_node("cache_guard", self._cache_guard)
            g.add_node("memory_guard", self._memory_guard)
            g.add_node("context_guard", self._context_guard)
            g.add_conditional_edges(
                "cache_lookup",
                self._after_lookup,
                ["cache_guard", "deliver_cached", "memory_recall"],
            )
            g.add_conditional_edges(
                "cache_guard", self._after_guard, ["deliver_cached", "memory_recall"]
            )
            g.add_edge("memory_recall", "memory_guard")
            g.add_edge("memory_guard", "retrieve")
            g.add_edge("retrieve", "build_contexts")
            g.add_edge("build_contexts", "context_guard")
            g.add_edge("context_guard", "generate")
        else:
            g.add_conditional_edges(
                "cache_lookup", self._after_lookup, ["deliver_cached", "memory_recall"]
            )
            g.add_edge("memory_recall", "retrieve")
            g.add_edge("retrieve", "build_contexts")
            g.add_edge("build_contexts", "generate")
        g.add_edge("generate", "write_back")
        g.add_edge("write_back", "record")
        g.add_edge("deliver_cached", "record")
        g.add_edge("record", END)
        return g.compile(checkpointer=checkpointer)
