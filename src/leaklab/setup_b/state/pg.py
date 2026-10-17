"""PgRepository: the StateRepository on PostgreSQL (psycopg 3).

Schema (created idempotently by PgRepository, in its own Postgres schema so tests are isolated):
  poller_state(name, link, last_success_at, last_attempt_at, last_error)
  membership_edges(group_id, member_kind, member_id)       direct membership (what Graph delta says)
  user_groups(user_id, group_id)                           the transitive CLOSURE (who is in what)
  directory(user_id, upn)
  acl_items(item_id, name, parent_id, is_folder, web_url, unique_perms, scope_id)
  acl_grants(scope_id, kind, ref, role, via)
  events(id, source, kind, subject, detail jsonb, seen_at, applied_at)
Times are epoch seconds (double precision), the same clock as the generator's mutation log.

One connection, guarded by a lock: the pollers run one after the other. Every method runs in a
transaction; atomic() groups several into one (nested use becomes a savepoint).
"""

import json
import re
import threading
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any

import psycopg

from leaklab.setup_b.state.model import (
    AclSnapshot,
    DocRecord,
    Edge,
    Event,
    Grant,
    GrantKind,
    MemberKind,
    PollerState,
)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS poller_state (
    name text PRIMARY KEY, link text, last_success_at double precision,
    last_attempt_at double precision, last_error text);
CREATE TABLE IF NOT EXISTS membership_edges (
    group_id text NOT NULL, member_kind text NOT NULL, member_id text NOT NULL,
    PRIMARY KEY (group_id, member_kind, member_id));
CREATE TABLE IF NOT EXISTS user_groups (
    user_id text NOT NULL, group_id text NOT NULL, PRIMARY KEY (user_id, group_id));
CREATE TABLE IF NOT EXISTS directory (user_id text PRIMARY KEY, upn text NOT NULL);
CREATE TABLE IF NOT EXISTS acl_items (
    item_id text PRIMARY KEY, name text NOT NULL, parent_id text, is_folder boolean NOT NULL,
    web_url text, unique_perms boolean NOT NULL, scope_id text NOT NULL);
CREATE TABLE IF NOT EXISTS acl_grants (
    scope_id text NOT NULL, kind text NOT NULL, ref text NOT NULL, role text NOT NULL,
    via text NOT NULL, PRIMARY KEY (scope_id, kind, ref, role, via));
CREATE TABLE IF NOT EXISTS events (
    id bigserial PRIMARY KEY, source text NOT NULL, kind text NOT NULL, subject text NOT NULL,
    detail jsonb NOT NULL, seen_at double precision NOT NULL, applied_at double precision NOT NULL);
"""
_NAME = re.compile(r"^[a-z_][a-z0-9_]*$")


class PgRepository:
    def __init__(self, dsn: str, schema: str = "leaklab_state") -> None:
        if not _NAME.match(schema):
            raise ValueError(f"unsafe schema name {schema!r}")
        self._dsn = dsn
        self._schema = schema
        self._lock = threading.RLock()
        with psycopg.connect(dsn, autocommit=True) as boot:
            boot.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
        self._conn = psycopg.connect(dsn, options=f"-c search_path={schema}")
        with self._conn.transaction():
            self._conn.execute(SCHEMA_SQL)

    def close(self) -> None:
        self._conn.close()

    def drop_schema(self) -> None:
        """Remove everything this repository created (used by tests)."""
        with self._lock:
            self._conn.close()
        with psycopg.connect(self._dsn, autocommit=True) as boot:
            boot.execute(f'DROP SCHEMA IF EXISTS "{self._schema}" CASCADE')

    @contextmanager
    def atomic(self) -> Iterator[None]:
        with self._lock, self._conn.transaction():
            yield

    def _exec(self, sql: str, params: Sequence[Any] = ()) -> list[tuple[Any, ...]]:
        with self._lock, self._conn.transaction():
            cur = self._conn.execute(sql, params)
            return cur.fetchall() if cur.description else []

    def _many(self, sql: str, rows: Sequence[Sequence[Any]]) -> None:
        if not rows:
            return
        with self._lock, self._conn.transaction(), self._conn.cursor() as cur:
            cur.executemany(sql, rows)

    # -- poller bookkeeping --------------------------------------------------------------------

    def get_state(self, name: str) -> PollerState | None:
        rows = self._exec(
            "SELECT link, last_success_at, last_attempt_at, last_error FROM poller_state "
            "WHERE name = %s",
            (name,),
        )
        return PollerState(name, *rows[0]) if rows else None

    def set_state(self, state: PollerState) -> None:
        self._exec(
            "INSERT INTO poller_state VALUES (%s, %s, %s, %s, %s) ON CONFLICT (name) DO UPDATE "
            "SET link = EXCLUDED.link, last_success_at = EXCLUDED.last_success_at, "
            "last_attempt_at = EXCLUDED.last_attempt_at, last_error = EXCLUDED.last_error",
            (
                state.name,
                state.link,
                state.last_success_at,
                state.last_attempt_at,
                state.last_error,
            ),
        )

    # -- membership ----------------------------------------------------------------------------

    def load_edges(self) -> set[Edge]:
        rows = self._exec("SELECT group_id, member_kind, member_id FROM membership_edges")
        return {Edge(g, _kind(k), m) for g, k, m in rows}

    def save_edges(self, edges: set[Edge], closure: Mapping[str, set[str]]) -> None:
        with self.atomic():
            self._exec("DELETE FROM membership_edges")
            self._exec("DELETE FROM user_groups")
            self._many(
                "INSERT INTO membership_edges VALUES (%s, %s, %s)",
                [(e.group_id, e.member_kind, e.member_id) for e in edges],
            )
            self._many(
                "INSERT INTO user_groups VALUES (%s, %s)",
                [(u, g) for u, groups in closure.items() for g in groups],
            )

    def groups_of(self, user_id: str) -> set[str]:
        rows = self._exec("SELECT group_id FROM user_groups WHERE user_id = %s", (user_id,))
        return {r[0] for r in rows}

    def load_directory(self) -> dict[str, str]:
        return {u: upn for u, upn in self._exec("SELECT user_id, upn FROM directory")}

    def upn_of(self, user_id: str) -> str | None:
        rows = self._exec("SELECT upn FROM directory WHERE user_id = %s", (user_id,))
        return str(rows[0][0]) if rows else None

    def save_directory(self, users: Mapping[str, str]) -> None:
        with self.atomic():
            self._exec("DELETE FROM directory")
            self._many("INSERT INTO directory VALUES (%s, %s)", list(users.items()))

    # -- ACL -----------------------------------------------------------------------------------

    def load_acl(self) -> AclSnapshot:
        snap = AclSnapshot()
        for item_id, name, parent, folder, url, unique, _scope in self._exec(
            "SELECT item_id, name, parent_id, is_folder, web_url, unique_perms, scope_id "
            "FROM acl_items"
        ):
            snap.items[item_id] = DocRecord(item_id, name, parent, folder, url, unique)
        grants: dict[str, list[Grant]] = {}
        for scope, kind, ref, role, via in self._exec(
            "SELECT scope_id, kind, ref, role, via FROM acl_grants "
            "ORDER BY scope_id, kind, ref, role, via"
        ):
            grants.setdefault(scope, []).append(Grant(_grant_kind(kind), ref, role, via))
        snap.grants = {s: tuple(g) for s, g in grants.items()}
        return snap

    def save_acl(self, snapshot: AclSnapshot) -> None:
        with self.atomic():
            self._exec("DELETE FROM acl_items")
            self._exec("DELETE FROM acl_grants")
            self._many(
                "INSERT INTO acl_items VALUES (%s, %s, %s, %s, %s, %s, %s)",
                [
                    (
                        i.doc_id,
                        i.name,
                        i.parent_id,
                        i.is_folder,
                        i.web_url,
                        i.unique,
                        snapshot.scope_of(i.doc_id),
                    )
                    for i in snapshot.items.values()
                ],
            )
            self._many(
                "INSERT INTO acl_grants VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                [
                    (scope, g.kind, g.ref, g.role, g.via)
                    for scope, grants in snapshot.grants.items()
                    for g in grants
                ],
            )

    def scope_of_item(self, item_id: str) -> str | None:
        rows = self._exec("SELECT scope_id FROM acl_items WHERE item_id = %s", (item_id,))
        return str(rows[0][0]) if rows else None

    def grants_of_scope(self, scope_id: str) -> tuple[Grant, ...]:
        rows = self._exec(
            "SELECT kind, ref, role, via FROM acl_grants WHERE scope_id = %s "
            "ORDER BY kind, ref, role, via",
            (scope_id,),
        )
        return tuple(Grant(_grant_kind(k), r, ro, v) for k, r, ro, v in rows)

    # -- events --------------------------------------------------------------------------------

    def append_events(self, events: Sequence[Event]) -> None:
        self._many(
            "INSERT INTO events (source, kind, subject, detail, seen_at, applied_at) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            [
                (e.source, e.kind, e.subject, json.dumps(e.detail), e.seen_at, e.applied_at)
                for e in events
            ],
        )

    def events(self, source: str | None = None) -> list[Event]:
        rows = self._exec(
            "SELECT source, kind, subject, detail, seen_at, applied_at FROM events "
            "WHERE (%s::text IS NULL OR source = %s) ORDER BY id",
            (source, source),
        )
        return [Event(s, k, sub, dict(d), seen, applied) for s, k, sub, d, seen, applied in rows]


def _kind(value: str) -> MemberKind:
    if value == "user":
        return "user"
    if value == "group":
        return "group"
    raise ValueError(f"unknown member kind {value!r}")


def _grant_kind(value: str) -> GrantKind:
    if value == "user":
        return "user"
    if value == "entra_group":
        return "entra_group"
    if value == "unknown":
        return "unknown"
    raise ValueError(f"unknown grant kind {value!r}")
