"""Who owns which conversation thread.

A LangGraph thread (the checkpointer's `thread_id`) holds one user's conversation: the questions and
the answers, which may quote documents only that user may read. The checkpointer itself lets
anybody read any thread id (verified 2026-10-18: `get_state` / `get_state_history` take only the id),
so ownership is kept here and enforced by the agent in GUARDED mode:

  claim(thread_id, user)   a new thread belongs to its first user; an existing one is the owner's
  history(user, thread_id) another user's thread is denied (ThreadDenied), a missing one is empty

NAIVE mode records the owner too but never enforces it: threads are shared.
"""

import threading
from typing import Protocol

from leaklab.agent.stores import AgentDb


class ThreadDenied(PermissionError):
    """The user asked for a thread that belongs to somebody else."""


class ThreadRegistry(Protocol):
    def claim(self, thread_id: str, user_oid: str, now: float) -> bool:
        """True if `user_oid` owns the thread afterwards (a new thread is claimed by the caller)."""
        ...

    def owner_of(self, thread_id: str) -> str | None: ...

    def clear(self) -> None: ...


class MemoryThreads:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._owners: dict[str, str] = {}

    def claim(self, thread_id: str, user_oid: str, now: float) -> bool:
        with self._lock:
            return self._owners.setdefault(thread_id, user_oid) == user_oid

    def owner_of(self, thread_id: str) -> str | None:
        with self._lock:
            return self._owners.get(thread_id)

    def clear(self) -> None:
        with self._lock:
            self._owners = {}


class PgThreads:
    def __init__(self, db: AgentDb) -> None:
        self._db = db

    def claim(self, thread_id: str, user_oid: str, now: float) -> bool:
        with self._db.tx() as conn:
            conn.execute(
                "INSERT INTO threads (thread_id, owner, created) VALUES (%s, %s, %s) "
                "ON CONFLICT (thread_id) DO NOTHING",
                (thread_id, user_oid, now),
            )
            row = conn.execute(
                "SELECT owner FROM threads WHERE thread_id = %s", (thread_id,)
            ).fetchone()
        return row is not None and row[0] == user_oid

    def owner_of(self, thread_id: str) -> str | None:
        with self._db.tx() as conn:
            row = conn.execute(
                "SELECT owner FROM threads WHERE thread_id = %s", (thread_id,)
            ).fetchone()
        return None if row is None else str(row[0])

    def clear(self) -> None:
        with self._db.tx() as conn:
            conn.execute("TRUNCATE threads")
