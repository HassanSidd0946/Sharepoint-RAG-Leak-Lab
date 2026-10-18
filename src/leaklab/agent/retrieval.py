"""Retrieval adapters: how the agent gets documents for a user. Setup A and Setup B.

  SetupARetriever   the ACL-filtered index, trimmed by the persona's own token (Setup A, basic)
  SetupBRetriever   Setup B's POST /query with the persona's token (state-filtered, adaptive paging)

Both return the same thing: Passages (source item id, name, full text) that the SYSTEM decided the
user may read. They raise RetrievalError for an operational problem, never an empty list that
could be mistaken for "nothing to find". The agent adds nothing to retrieval's access decisions in
NAIVE mode; in GUARDED mode it re-checks every source again (guard.py).
"""

from collections.abc import Callable
from typing import Any, Protocol

import httpx

from leaklab.agent.types import Passage, UserCtx
from leaklab.setup_a.client import SearchPort
from leaklab.setup_a.keys import item_id_of_key


class RetrievalError(Exception):
    """The retrieval could not answer (HTTP error, bad answer, transport problem)."""


class Retriever(Protocol):
    name: str

    def retrieve(self, user: UserCtx, question: str, k: int) -> list[Passage]: ...


class SetupARetriever:
    name = "setup_a"

    def __init__(self, search: SearchPort, tokens: Callable[[str], str]) -> None:
        self._search = search
        self._tokens = tokens  # upn -> the persona's token for the search scope

    def retrieve(self, user: UserCtx, question: str, k: int) -> list[Passage]:
        try:
            token = self._tokens(user.upn)
        except Exception as exc:
            raise RetrievalError(f"token:{type(exc).__name__}") from None
        status, answer = self._search.query(token, question)
        if status != 200 or not isinstance(answer, list):
            raise RetrievalError(f"http:{status}")
        passages: list[Passage] = []
        for doc in answer:
            try:
                item = item_id_of_key(str(doc["id"]))
            except (KeyError, ValueError):
                continue  # a hit whose key we cannot map to a drive item is not usable
            passages.append(
                Passage(
                    item,
                    str(doc.get("metadata_spo_item_name") or ""),
                    str(doc.get("content") or ""),
                )
            )
        return passages[:k]


class SetupBRetriever:
    name = "setup_b"

    def __init__(
        self, base_url: str, tokens: Callable[[str], str], http: httpx.Client | None = None
    ) -> None:
        self._base = base_url.rstrip("/")
        self._tokens = tokens
        self._http = http or httpx.Client(timeout=60.0)

    def retrieve(self, user: UserCtx, question: str, k: int) -> list[Passage]:
        try:
            token = self._tokens(user.upn)
            resp = self._http.post(
                f"{self._base}/query", json={"persona_token": token, "query": question, "k": k}
            )
        except httpx.TransportError as exc:
            raise RetrievalError(f"transport:{type(exc).__name__}") from None
        except Exception as exc:
            raise RetrievalError(f"token:{type(exc).__name__}") from None
        if resp.status_code != 200:
            raise RetrievalError(f"http:{resp.status_code}")
        try:
            body: dict[str, Any] = resp.json()
            return [
                Passage(str(r["doc_id"]), str(r.get("name") or ""), str(r.get("content") or ""))
                for r in body["results"]
            ][:k]
        except (ValueError, KeyError, TypeError):
            raise RetrievalError("bad-answer") from None
