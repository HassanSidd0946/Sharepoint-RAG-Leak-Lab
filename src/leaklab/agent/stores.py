"""The semantic cache and the long-term memory, in memory (tests) and in Postgres + pgvector.

SemanticCache.lookup(embedding) returns the NEAREST entry and its cosine similarity; deciding
whether that is a hit (similarity >= threshold) belongs to the agent, which also journals every
lookup. MemoryStore.search returns the nearest items with their similarity.

Both stores hold whatever the agent gives them. In NAIVE mode that is text only (sources and owner
are None: "no permission metadata"); in GUARDED mode every entry carries the sources it was made
from and the generation of each. The stores themselves enforce nothing.

Postgres: one schema (default leaklab_agent) with tables cache, memory and threads, a `vector(N)`
column and cosine distance (`<=>`); the extension is created if missing. The embedded server of
`pgserver` ships pgvector 0.6.2 (verified 2026-10-18); the docker-compose Postgres image does NOT,
so the benchmark points LEAKLAB_AGENT_PG_DSN at an embedded or pgvector-enabled server.
"""

import json
import re
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any, Protocol

import psycopg

from leaklab.agent.embedding import DIMS, cosine
from leaklab.agent.types import CacheEntry, MemoryItem, SourceRef

_NAME = re.compile(r"^[a-z_][a-z0-9_]*$")


class SemanticCache(Protocol):
    def lookup(self, embedding: list[float]) -> tuple[CacheEntry | None, float]: ...

    def store(self, entry: CacheEntry) -> int: ...

    def entries(self) -> list[CacheEntry]: ...

    def clear(self) -> None: ...


class MemoryStore(Protocol):
    def add(self, item: MemoryItem) -> int: ...

    def search(
        self, embedding: list[float], limit: int, min_similarity: float
    ) -> list[tuple[MemoryItem, float]]: ...

    def items(self) -> list[MemoryItem]: ...

    def clear(self) -> None: ...


# -- in memory --------------------------------------------------------------------------------------


class MemoryCache:
    def __init__(self) -> None:
        self._entries: list[CacheEntry] = []

    def lookup(self, embedding: list[float]) -> tuple[CacheEntry | None, float]:
        best: tuple[CacheEntry | None, float] = (None, 0.0)
        for entry in self._entries:
            sim = cosine(embedding, entry.embedding)
            if best[0] is None or sim > best[1]:
                best = (entry, sim)
        return best

    def store(self, entry: CacheEntry) -> int:
        entry.id = len(self._entries) + 1
        self._entries.append(entry)
        return entry.id

    def entries(self) -> list[CacheEntry]:
        return list(self._entries)

    def clear(self) -> None:
        self._entries = []


class MemoryMemory:
    def __init__(self) -> None:
        self._items: list[MemoryItem] = []

    def add(self, item: MemoryItem) -> int:
        item.id = len(self._items) + 1
        self._items.append(item)
        return item.id

    def search(
        self, embedding: list[float], limit: int, min_similarity: float
    ) -> list[tuple[MemoryItem, float]]:
        scored = [(i, cosine(embedding, i.embedding)) for i in self._items]
        scored = [(i, s) for i, s in scored if s >= min_similarity]
        return sorted(scored, key=lambda p: -p[1])[:limit]

    def items(self) -> list[MemoryItem]:
        return list(self._items)

    def clear(self) -> None:
        self._items = []


# -- Postgres + pgvector ----------------------------------------------------------------------------


class AgentDb:
    """One connection (with a lock) to the agent's schema; creates the extension and the tables."""

    def __init__(self, dsn: str, schema: str = "leaklab_agent", dims: int = DIMS) -> None:
        if not _NAME.match(schema):
            raise ValueError(f"unsafe schema name {schema!r}")
        self.dsn, self.schema, self.dims = dsn, schema, dims
        self._lock = threading.RLock()
        try:
            with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as boot:
                boot.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
                try:
                    boot.execute("CREATE EXTENSION IF NOT EXISTS vector")
                except psycopg.Error as exc:
                    raise RuntimeError(
                        "this Postgres has no pgvector extension (the docker-compose image does not): "
                        "use the embedded server or a pgvector image via LEAKLAB_AGENT_PG_DSN"
                    ) from exc
        except (psycopg.OperationalError, psycopg.ProgrammingError) as exc:
            raise RuntimeError(
                f"cannot reach Postgres for the agent ({type(exc).__name__})"
            ) from None
        self.conn = psycopg.connect(
            dsn, options=f"-c search_path={schema},public", connect_timeout=10
        )
        with self.conn.transaction():
            self.conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS cache (
                    id bigserial PRIMARY KEY, question text NOT NULL, embedding vector({dims}) NOT NULL,
                    answer text NOT NULL, sources jsonb, owner text, created double precision NOT NULL);
                CREATE TABLE IF NOT EXISTS memory (
                    id bigserial PRIMARY KEY, text text NOT NULL, embedding vector({dims}) NOT NULL,
                    sources jsonb, owner text, created double precision NOT NULL);
                CREATE TABLE IF NOT EXISTS threads (
                    thread_id text PRIMARY KEY, owner text NOT NULL, created double precision NOT NULL);
                """
            )

    @contextmanager
    def tx(self) -> Iterator[psycopg.Connection[Any]]:
        with self._lock, self.conn.transaction():
            yield self.conn

    def close(self) -> None:
        self.conn.close()

    def drop_schema(self) -> None:
        with self._lock:
            self.conn.close()
        with psycopg.connect(self.dsn, autocommit=True) as boot:
            boot.execute(f'DROP SCHEMA IF EXISTS "{self.schema}" CASCADE')


def vector_literal(embedding: Sequence[float], dims: int) -> str:
    if len(embedding) != dims:
        raise ValueError(f"expected {dims} dimensions, got {len(embedding)}")
    return "[" + ",".join(repr(float(x)) for x in embedding) + "]"


def _sources_json(sources: tuple[SourceRef, ...] | None) -> str | None:
    if sources is None:
        return None
    return json.dumps([{"item_id": s.item_id, "generation": s.generation} for s in sources])


def _sources_of(raw: Any) -> tuple[SourceRef, ...] | None:
    if raw is None:
        return None
    return tuple(SourceRef(str(s["item_id"]), str(s["generation"])) for s in raw)


class PgSemanticCache:
    def __init__(self, db: AgentDb) -> None:
        self._db = db

    def lookup(self, embedding: list[float]) -> tuple[CacheEntry | None, float]:
        vec = vector_literal(embedding, self._db.dims)
        with self._db.tx() as conn:
            row = conn.execute(
                "SELECT id, question, answer, sources, owner, created, 1 - (embedding <=> %s::vector) "
                "FROM cache ORDER BY embedding <=> %s::vector LIMIT 1",
                (vec, vec),
            ).fetchone()
        if row is None:
            return None, 0.0
        entry = CacheEntry(
            question=row[1],
            embedding=[],
            answer=row[2],
            sources=_sources_of(row[3]),
            owner=row[4],
            created=row[5],
            id=row[0],
        )
        return entry, float(row[6])

    def store(self, entry: CacheEntry) -> int:
        with self._db.tx() as conn:
            row = conn.execute(
                "INSERT INTO cache (question, embedding, answer, sources, owner, created) "
                "VALUES (%s, %s::vector, %s, %s::jsonb, %s, %s) RETURNING id",
                (
                    entry.question,
                    vector_literal(entry.embedding, self._db.dims),
                    entry.answer,
                    _sources_json(entry.sources),
                    entry.owner,
                    entry.created,
                ),
            ).fetchone()
        assert row is not None
        entry.id = int(row[0])
        return entry.id

    def entries(self) -> list[CacheEntry]:
        with self._db.tx() as conn:
            rows = conn.execute(
                "SELECT id, question, answer, sources, owner, created FROM cache ORDER BY id"
            ).fetchall()
        return [CacheEntry(r[1], [], r[2], _sources_of(r[3]), r[4], r[5], int(r[0])) for r in rows]

    def clear(self) -> None:
        with self._db.tx() as conn:
            conn.execute("TRUNCATE cache RESTART IDENTITY")


class PgMemoryStore:
    def __init__(self, db: AgentDb) -> None:
        self._db = db

    def add(self, item: MemoryItem) -> int:
        with self._db.tx() as conn:
            row = conn.execute(
                "INSERT INTO memory (text, embedding, sources, owner, created) "
                "VALUES (%s, %s::vector, %s::jsonb, %s, %s) RETURNING id",
                (
                    item.text,
                    vector_literal(item.embedding, self._db.dims),
                    _sources_json(item.sources),
                    item.owner,
                    item.created,
                ),
            ).fetchone()
        assert row is not None
        item.id = int(row[0])
        return item.id

    def search(
        self, embedding: list[float], limit: int, min_similarity: float
    ) -> list[tuple[MemoryItem, float]]:
        vec = vector_literal(embedding, self._db.dims)
        with self._db.tx() as conn:
            rows = conn.execute(
                "SELECT id, text, sources, owner, created, 1 - (embedding <=> %s::vector) AS sim "
                "FROM memory ORDER BY embedding <=> %s::vector LIMIT %s",
                (vec, vec, limit),
            ).fetchall()
        return [
            (MemoryItem(r[1], [], _sources_of(r[2]), r[3], r[4], int(r[0])), float(r[5]))
            for r in rows
            if float(r[5]) >= min_similarity
        ]

    def items(self) -> list[MemoryItem]:
        with self._db.tx() as conn:
            rows = conn.execute(
                "SELECT id, text, sources, owner, created FROM memory ORDER BY id"
            ).fetchall()
        return [MemoryItem(r[1], [], _sources_of(r[2]), r[3], r[4], int(r[0])) for r in rows]

    def clear(self) -> None:
        with self._db.tx() as conn:
            conn.execute("TRUNCATE memory RESTART IDENTITY")
