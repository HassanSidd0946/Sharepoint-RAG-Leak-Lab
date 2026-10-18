"""Test scaffolding for the Setup B API: signing keys and tokens, a fake index, a wired service.

The fake index holds one document per canary of the spec (content contains the canary) and knows
nothing about permissions, like ll-sp-index-b. It can fail on demand and can carry a (deliberately
wrong) stored ACL, to prove no decision reads it.
"""

import time
from typing import Any

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from leaklab.runner.tenant import resolve_items
from leaklab.setup_a.client import IndexerRun
from leaklab.setup_b.api.app import create_app
from leaklab.setup_b.api.decisionlog import DecisionLog, MemoryDecisionLog
from leaklab.setup_b.api.search import Fetched, Hit, IndexAcl, IndexUnavailable
from leaklab.setup_b.api.service import QueryService
from leaklab.setup_b.api.tokens import TokenValidator
from leaklab.spec import generate_canaries
from runner_fakes import SPEC
from setup_b_world import World

TENANT = "11111111-2222-3333-4444-555555555555"
AUDIENCE = "https://search.azure.com"
OPERATOR_KEY = "operator-key-for-tests"
CANARIES = generate_canaries(SPEC)
P = f"CANARY-{SPEC.naming.generation}-"  # prefix of every canary of the test generation
ALL = [c for cs in CANARIES.values() for c in cs]


class Keys:
    """One RSA key pair; `key_for` is the KeySource the validator asks."""

    def __init__(self, kid: str = "k1") -> None:
        self.kid = kid
        self.private = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def key_for(self, kid: str) -> Any | None:
        return self.private.public_key() if kid == self.kid else None

    def token(
        self,
        oid: str | None,
        upn: str | None = "someone@example.test",
        *,
        key: Any = None,
        kid: str | None = None,
        **overrides: Any,
    ) -> str:
        now = int(time.time())
        claims: dict[str, Any] = {
            "iss": f"https://sts.windows.net/{TENANT}/",
            "aud": AUDIENCE,
            "tid": TENANT,
            "oid": oid,
            "upn": upn,
            "scp": "user_impersonation",
            "iat": now,
            "exp": now + 3600,
        }
        claims.update(overrides)
        claims = {k: v for k, v in claims.items() if v is not None}
        return jwt.encode(
            claims, key or self.private, algorithm="RS256", headers={"kid": kid or self.kid}
        )


class FakeIndex:
    def __init__(self, world: World) -> None:
        items = resolve_items(world.env, ALL)
        self.docs = [
            Hit(str(items[c]["id"]), str(items[c]["name"]), f"content of {c}") for c in ALL
        ]
        self.canary_of = {h.doc_id: c for h, c in zip(self.docs, ALL, strict=True)}
        self.fail: Exception | None = None
        self.stored: dict[str, IndexAcl] = {}
        self.queries: list[tuple[str, int]] = []

    def query(self, text: str, top: int) -> Fetched:
        self.queries.append((text, top))
        if self.fail:
            raise self.fail
        needle = text.strip('"')
        hits = [d for d in self.docs if needle == "*" or needle in d.content or needle == "CANARY"]
        return Fetched(hits[:top], 0, len(hits))

    def stored_acl(self, doc_id: str) -> IndexAcl | None:
        return self.stored.get(doc_id)

    def indexer_runs(self) -> list[IndexerRun]:
        return []

    def close(self) -> None:
        pass


class Api:
    """A World, a fake index, a validator over test keys, a decision log and a TestClient."""

    def __init__(
        self,
        world: World,
        *,
        log: DecisionLog | None = None,
        over_fetch: int = 5,
        allow_debug: bool = True,
        operator_key: str = OPERATOR_KEY,
    ) -> None:
        self.world = world
        self.keys = Keys()
        self.index = FakeIndex(world)
        self.log = log or MemoryDecisionLog()
        self.service = QueryService(
            reader=world.reader,
            repo=world.repo,
            index=self.index,
            log=self.log,
            validator=TokenValidator(tenant_id=TENANT, audience=AUDIENCE, keys=self.keys),
            clock=world.clock.time,
            over_fetch=over_fetch,
            readiness=world.readiness,
        )
        self.client = TestClient(
            create_app(self.service, operator_key=operator_key, allow_debug=allow_debug),
            raise_server_exceptions=False,
        )

    def token_for(self, persona: str) -> str:
        return self.keys.token(self.world.uid(persona), f"{persona}@example.test")

    def query(
        self, persona: str, text: str = "CANARY", k: int = 50, **extra: Any
    ) -> dict[str, Any]:
        resp = self.client.post(
            "/query",
            json={"persona_token": self.token_for(persona), "query": text, "k": k, **extra},
        )
        assert resp.status_code == 200, resp.text
        body: dict[str, Any] = resp.json()
        return body

    def served_canaries(self, persona: str, text: str = "CANARY", k: int = 50) -> set[str]:
        body = self.query(persona, text, k)
        return {self.index.canary_of[r["doc_id"]] for r in body["results"]}


def operator() -> dict[str, str]:
    return {"X-Operator-Key": OPERATOR_KEY}


__all__ = ["Api", "Keys", "FakeIndex", "IndexUnavailable", "operator", "ALL", "CANARIES"]
