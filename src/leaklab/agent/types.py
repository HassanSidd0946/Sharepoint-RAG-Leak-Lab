"""Plain data of the agent: who asks, what was retrieved, what the cache and memory hold.

Identities: `UserCtx.oid` is the Entra object id (the key of Setup B's state); `persona` is the
spec id (p-finlead) used for reports and the oracle. A SourceRef names ONE source document by its
Graph drive item id plus the tenant generation it belongs to: an item id of an earlier generation
must never be trusted, so the generation travels with it.
"""

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class UserCtx:
    persona: str  # spec id, e.g. "p-finlead"
    oid: str  # Entra object id (state key)
    upn: str  # sign-in name (to get the persona's token)


@dataclass(frozen=True)
class SourceRef:
    item_id: str  # Graph drive item id of the source document
    generation: str  # tenant generation the id belongs to


@dataclass(frozen=True)
class Passage:
    """One retrieved document: the id of its source, its name and its full text."""

    item_id: str
    name: str
    content: str


@dataclass(frozen=True)
class Context:
    """Text handed to the LLM, with where it came from (kind: retrieved | memory)."""

    kind: str
    text: str
    sources: tuple[SourceRef, ...] | None  # None: unknown (the NAIVE stores keep no metadata)


@dataclass
class CacheEntry:
    """A cached answer. NAIVE: `sources` and `owner` are None (no permission metadata).
    GUARDED: `sources` lists EVERY source that went into the answer; an empty or missing list
    means "unknown" and is never served."""

    question: str
    embedding: list[float]
    answer: str
    sources: tuple[SourceRef, ...] | None = None
    owner: str | None = None  # oid of the user the answer was made for
    created: float = 0.0
    id: int | None = None


@dataclass
class MemoryItem:
    """One remembered fact (a line extracted from an answer). Same metadata rule as CacheEntry."""

    text: str
    embedding: list[float]
    sources: tuple[SourceRef, ...] | None = None
    owner: str | None = None
    created: float = 0.0
    id: int | None = None


@dataclass
class Delivery:
    """What the user received for one question, and how it came about."""

    answer: str
    via: str  # "cache" | "llm"
    cache: dict[str, Any] = field(default_factory=dict)  # lookup record (status, similarity, ...)
    memory: list[dict[str, Any]] = field(default_factory=list)  # memory items recalled
    passages: list[str] = field(default_factory=list)  # item ids retrieved
    guard: list[dict[str, Any]] = field(default_factory=list)  # guard decisions
    thread_id: str | None = None
