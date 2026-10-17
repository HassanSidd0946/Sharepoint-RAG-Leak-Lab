"""Where the authoritative state lives: the StateRepository protocol and an in-memory version.

The pollers and the access reader talk only to this protocol. MemoryRepository is the reference (and
what fast tests use); PgRepository (pg.py) is the real store. tests/test_setup_b_repository.py runs
the SAME contract tests against both, so they cannot drift apart.

Every write the pollers make is one `atomic()` block: the state, the events and the poller's
last_success_at commit together or not at all (a crash never leaves a change applied but unlogged,
or logged but not applied).
"""

import copy
from collections.abc import Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from typing import Protocol

from leaklab.setup_b.state.model import AclSnapshot, Edge, Event, Grant, PollerState


class StateRepository(Protocol):
    def atomic(self) -> AbstractContextManager[None]: ...

    # poller bookkeeping
    def get_state(self, name: str) -> PollerState | None: ...
    def set_state(self, state: PollerState) -> None: ...

    # membership
    def load_edges(self) -> set[Edge]: ...
    def save_edges(self, edges: set[Edge], closure: Mapping[str, set[str]]) -> None: ...
    def groups_of(self, user_id: str) -> set[str]: ...
    def load_directory(self) -> dict[str, str]: ...
    def upn_of(self, user_id: str) -> str | None: ...
    def save_directory(self, users: Mapping[str, str]) -> None: ...

    # ACL
    def load_acl(self) -> AclSnapshot: ...
    def save_acl(self, snapshot: AclSnapshot) -> None: ...
    def scope_of_item(self, item_id: str) -> str | None: ...
    def grants_of_scope(self, scope_id: str) -> tuple[Grant, ...]: ...

    # event log
    def append_events(self, events: Sequence[Event]) -> None: ...
    def events(self, source: str | None = None) -> list[Event]: ...


class MemoryRepository:
    def __init__(self) -> None:
        self._states: dict[str, PollerState] = {}
        self._edges: set[Edge] = set()
        self._closure: dict[str, set[str]] = {}
        self._directory: dict[str, str] = {}
        self._acl = AclSnapshot()
        self._scopes: dict[str, str] = {}  # item id -> scope id, computed when saved
        self._events: list[Event] = []

    @contextmanager
    def atomic(self) -> Iterator[None]:
        saved = copy.deepcopy(self.__dict__)
        try:
            yield
        except BaseException:
            self.__dict__.clear()
            self.__dict__.update(saved)
            raise

    def get_state(self, name: str) -> PollerState | None:
        return self._states.get(name)

    def set_state(self, state: PollerState) -> None:
        self._states[state.name] = state

    def load_edges(self) -> set[Edge]:
        return set(self._edges)

    def save_edges(self, edges: set[Edge], closure: Mapping[str, set[str]]) -> None:
        self._edges = set(edges)
        self._closure = {u: set(g) for u, g in closure.items()}

    def groups_of(self, user_id: str) -> set[str]:
        return set(self._closure.get(user_id, ()))

    def load_directory(self) -> dict[str, str]:
        return dict(self._directory)

    def upn_of(self, user_id: str) -> str | None:
        return self._directory.get(user_id)

    def save_directory(self, users: Mapping[str, str]) -> None:
        self._directory = dict(users)

    def load_acl(self) -> AclSnapshot:
        return copy.deepcopy(self._acl)

    def save_acl(self, snapshot: AclSnapshot) -> None:
        self._acl = copy.deepcopy(snapshot)
        self._scopes = {i: snapshot.scope_of(i) for i in snapshot.items}

    def scope_of_item(self, item_id: str) -> str | None:
        return self._scopes.get(item_id)

    def grants_of_scope(self, scope_id: str) -> tuple[Grant, ...]:
        return self._acl.grants.get(scope_id, ())

    def append_events(self, events: Sequence[Event]) -> None:
        self._events.extend(events)

    def events(self, source: str | None = None) -> list[Event]:
        return [e for e in self._events if source is None or e.source == source]
