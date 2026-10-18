"""Fakes for the agent tests: documents with readers, a keyword retriever and an access check."""

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from leaklab.agent.embedding import HashEmbedder, tokens
from leaklab.agent.graph import Agent, AgentConfig
from leaklab.agent.guard import AccessResult
from leaklab.agent.llm import StubLLM
from leaklab.agent.retrieval import RetrievalError
from leaklab.agent.stores import MemoryCache, MemoryMemory
from leaklab.agent.threads import MemoryThreads
from leaklab.agent.types import Passage, UserCtx

GEN = "GTEST01"
LEADS = UserCtx("p-finlead", "oid-lead", "lead@x")
INTERN = UserCtx("p-intern", "oid-intern", "intern@x")
FINANA = UserCtx("p-finana", "oid-finana", "finana@x")


@dataclass
class Doc:
    item_id: str
    title: str
    topic: str
    canary: str
    readers: set[str] = field(default_factory=set)  # oids

    @property
    def text(self) -> str:
        return (
            f"{self.title}\nThis is a synthetic test document.\nTopic: {self.topic}\n"
            f"Reference code: {self.canary}\n851d57b7 8dc435be"
        )


FLD1 = Doc(
    "item-fld-01",
    "Finance/Leadership document 01",
    "salaries",
    f"CANARY-{GEN}-FLD-01-aaaaaa",
    {LEADS.oid},
)
FLD2 = Doc(
    "item-fld-02",
    "Finance/Leadership document 02",
    "incident review",
    f"CANARY-{GEN}-FLD-02-bbbbbb",
    {LEADS.oid},
)
GEN1 = Doc(
    "item-gen-01",
    "General document 01",
    "vendor contract",
    f"CANARY-{GEN}-GEN-01-cccccc",
    {LEADS.oid, INTERN.oid, FINANA.oid},
)
DOCS = [FLD1, FLD2, GEN1]

Q_SEED = "What does the Finance Leadership document 01 say about salaries?"
Q_PARA = "Tell me what the Finance Leadership document 01 says about salaries"
Q_GEN_SEED = "What does the General document 01 say about the vendor contract?"
Q_GEN_PARA = "Tell me what the General document 01 says about the vendor contract"
Q_MEM_SEED = "What does the Finance Leadership document 02 say about the incident review?"
Q_MEM_READER = "Any incident review notes from leadership?"


class FakeRetriever:
    """Keyword retrieval over DOCS that returns only what the user may read (like A and B do)."""

    name = "fake"

    def __init__(self, docs: Sequence[Doc] = DOCS, *, ignore_acl: bool = False) -> None:
        self.docs = list(docs)
        self.ignore_acl = ignore_acl  # a retrieval that does NOT trim (to test the context guard)
        self.fail = False
        self.calls: list[tuple[str, str]] = []

    def retrieve(self, user: UserCtx, question: str, k: int) -> list[Passage]:
        self.calls.append((user.persona, question))
        if self.fail:
            raise RetrievalError("http:503")
        wanted = set(tokens(question))
        scored = []
        for d in self.docs:
            if not self.ignore_acl and user.oid not in d.readers:
                continue
            score = len(wanted & set(tokens(d.title + " " + d.topic)))
            if score:
                scored.append((-score, d.title, d))
        scored.sort(key=lambda t: t[:2])
        return [Passage(d.item_id, d.title, d.text) for _, _, d in scored[:k]]


class FakeAccess:
    """Access from a table: item id -> readers. `generation` is the live one."""

    def __init__(self, docs: Sequence[Doc] = DOCS, generation: str = GEN) -> None:
        self.generation = generation
        self.readers = {d.item_id: set(d.readers) for d in docs}
        self.fail: Exception | None = None
        self.checked: list[tuple[str, tuple[str, ...]]] = []

    def check(self, user_oid: str, item_ids: Sequence[str]) -> list[AccessResult]:
        self.checked.append((user_oid, tuple(item_ids)))
        if self.fail:
            raise self.fail
        return [
            AccessResult(
                i,
                user_oid in self.readers.get(i, set()),
                "allowed"
                if user_oid in self.readers.get(i, set())
                else ("no_such_document" if i not in self.readers else "no_matching_grant"),
            )
            for i in item_ids
        ]


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        self.now += 1.0
        return self.now


def make_agent(
    mode: str,
    *,
    retriever: FakeRetriever | None = None,
    access: FakeAccess | None = None,
    cache: Any = None,
    memory: Any = None,
    threads: Any = None,
    checkpointer: Any = None,
    events: list[dict[str, Any]] | None = None,
    **config: Any,
) -> Agent:
    log = events if events is not None else []
    return Agent(
        config=AgentConfig(mode=mode, generation=GEN, **config),  # type: ignore[arg-type]
        retriever=retriever or FakeRetriever(),
        llm=StubLLM(),
        embedder=HashEmbedder(),
        cache=cache or MemoryCache(),
        memory=memory or MemoryMemory(),
        threads=threads or MemoryThreads(),
        clock=Clock(),
        access=(access or FakeAccess()) if mode == "guarded" else None,
        checkpointer=checkpointer,
        journal=log.append,
    )
