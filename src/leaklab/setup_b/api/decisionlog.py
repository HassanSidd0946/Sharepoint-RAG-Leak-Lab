"""The decision log: every allow/deny the API makes, in a hash chain, in memory or in Postgres.

Each record's hash is sha256(previous hash + the record's canonical JSON), starting from GENESIS.
Changing, deleting or reordering any record breaks the chain from that point on, and
`verify()` finds the first broken record. It is tamper-EVIDENT, not tamper-proof: someone who
can rewrite the whole table can recompute every hash (to prevent that, publish the head hash
somewhere else from time to time; see docs/ARCHITECTURE.md).

What is stored: who (object id), which document, allowed or not, the reason, the request id, a few
numbers in `detail`. NEVER stored: tokens, the query text (only its sha256), document content.
Appends are serialized (a lock in memory, an advisory lock in Postgres), so concurrent requests
still produce one chain.
"""

import hashlib
import json
import re
import threading
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Protocol

import psycopg

GENESIS = "0" * 64
_NAME = re.compile(r"^[a-z_][a-z0-9_]*$")
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS decisions (
    seq bigint PRIMARY KEY, ts double precision NOT NULL, kind text NOT NULL, user_id text NOT NULL,
    doc_id text NOT NULL, allowed boolean NOT NULL, reason text NOT NULL, request_id text NOT NULL,
    detail text NOT NULL, prev_hash text NOT NULL, hash text NOT NULL);
"""


@dataclass(frozen=True)
class Entry:
    """What the caller supplies; the log adds seq and the hashes."""

    ts: float
    kind: str  # "decision" (one document for one user) | "query" (the summary of a request)
    user_id: str
    doc_id: str  # "" for a query summary
    allowed: bool
    reason: str
    request_id: str
    detail: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Record:
    seq: int
    entry: Entry
    prev_hash: str
    hash: str


@dataclass(frozen=True)
class Verification:
    ok: bool
    checked: int
    first_bad_seq: int | None = None
    head: str = GENESIS


class DecisionLog(Protocol):
    def append(self, entries: Sequence[Entry]) -> None: ...

    def records(self, limit: int | None = None) -> list[Record]: ...

    def verify(self) -> Verification: ...

    def close(self) -> None: ...


def canonical(detail: Mapping[str, Any]) -> str:
    return json.dumps(detail, sort_keys=True, separators=(",", ":"), default=str)


def compute_hash(prev_hash: str, seq: int, entry: Entry) -> str:
    body = json.dumps(
        [
            seq,
            entry.ts,
            entry.kind,
            entry.user_id,
            entry.doc_id,
            entry.allowed,
            entry.reason,
            entry.request_id,
            canonical(entry.detail),
        ],
        separators=(",", ":"),
    )
    return hashlib.sha256(f"{prev_hash}\n{body}".encode()).hexdigest()


def verify_records(records: Sequence[Record]) -> Verification:
    """Recompute the chain over `records` (which must start at the beginning of the log)."""
    prev = GENESIS
    for expected_seq, rec in enumerate(records, start=1):
        if (
            rec.seq != expected_seq
            or rec.prev_hash != prev
            or rec.hash != compute_hash(prev, rec.seq, rec.entry)
        ):
            return Verification(False, expected_seq - 1, rec.seq, prev)
        prev = rec.hash
    return Verification(True, len(records), None, prev)


class MemoryDecisionLog:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: list[Record] = []

    def append(self, entries: Sequence[Entry]) -> None:
        with self._lock:
            prev = self._records[-1].hash if self._records else GENESIS
            for entry in entries:
                seq = len(self._records) + 1
                prev_hash, prev = prev, compute_hash(prev, seq, entry)
                self._records.append(Record(seq, entry, prev_hash, prev))

    def records(self, limit: int | None = None) -> list[Record]:
        with self._lock:
            return list(self._records if limit is None else self._records[-limit:])

    def verify(self) -> Verification:
        with self._lock:
            return verify_records(self._records)

    def close(self) -> None:
        pass


class PgDecisionLog:
    def __init__(self, dsn: str, schema: str = "leaklab_api") -> None:
        if not _NAME.match(schema):
            raise ValueError(f"unsafe schema name {schema!r}")
        self._dsn, self._schema = dsn, schema
        self._lock = threading.RLock()
        try:
            with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as boot:
                boot.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
        except (psycopg.OperationalError, psycopg.ProgrammingError) as exc:
            raise RuntimeError(
                f"cannot reach Postgres for the decision log ({type(exc).__name__})"
            ) from None  # never echo the DSN: it has the password
        self._conn = psycopg.connect(dsn, options=f"-c search_path={schema}", connect_timeout=10)
        with self._conn.transaction():
            self._conn.execute(SCHEMA_SQL)

    @contextmanager
    def _tx(self) -> Iterator[psycopg.Connection[Any]]:
        with self._lock, self._conn.transaction():
            yield self._conn

    def append(self, entries: Sequence[Entry]) -> None:
        if not entries:
            return
        with self._tx() as conn:
            conn.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))", (f"{self._schema}.decisions",)
            )
            row = conn.execute(
                "SELECT seq, hash FROM decisions ORDER BY seq DESC LIMIT 1"
            ).fetchone()
            seq, prev = (row[0], row[1]) if row else (0, GENESIS)
            for entry in entries:
                seq += 1
                digest = compute_hash(prev, seq, entry)
                conn.execute(
                    "INSERT INTO decisions VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        seq,
                        entry.ts,
                        entry.kind,
                        entry.user_id,
                        entry.doc_id,
                        entry.allowed,
                        entry.reason,
                        entry.request_id,
                        canonical(entry.detail),
                        prev,
                        digest,
                    ),
                )
                prev = digest

    def records(self, limit: int | None = None) -> list[Record]:
        sql = (
            "SELECT seq, ts, kind, user_id, doc_id, allowed, reason, request_id, detail, "
            "prev_hash, hash FROM decisions"
        )
        with self._tx() as conn:
            if limit is None:
                rows = conn.execute(sql + " ORDER BY seq").fetchall()
            else:
                rows = conn.execute(sql + " ORDER BY seq DESC LIMIT %s", (limit,)).fetchall()[::-1]
        return [
            Record(
                r[0],
                Entry(r[1], r[2], r[3], r[4], r[5], r[6], r[7], json.loads(r[8])),
                r[9],
                r[10],
            )
            for r in rows
        ]

    def verify(self) -> Verification:
        return verify_records(self.records())

    def close(self) -> None:
        self._conn.close()

    def drop_schema(self) -> None:
        with self._lock:
            self._conn.close()
        with psycopg.connect(self._dsn, autocommit=True) as boot:
            boot.execute(f'DROP SCHEMA IF EXISTS "{self._schema}" CASCADE')
