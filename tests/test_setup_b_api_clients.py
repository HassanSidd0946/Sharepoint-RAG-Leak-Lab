"""The two network clients of the API, against mock transports: the index (service identity, key
mapping, errors) and the JWKS key source (cache, throttled refetch, failure keeps the old keys)."""

import base64
import json
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from leaklab.setup_a.keys import doc_key, item_id_of_key
from leaklab.setup_b.api.search import HttpIndexPort, IndexUnavailable
from leaklab.setup_b.api.tokens import JwksKeys

SITE, DRIVE = "host,guid1,guid2", "b!drive"
SERVICE_TOKEN = "service-bearer-value"


def key_of(item_id: str) -> str:
    return doc_key(SITE, DRIVE, item_id)


def index_port(handler: Any, clock: Any = None) -> tuple[HttpIndexPort, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    port = HttpIndexPort(
        "https://svc.search.windows.net/",
        client=httpx.Client(transport=httpx.MockTransport(wrapped)),
        token_source=lambda: SERVICE_TOKEN,
        clock=clock or (lambda: 1000.0),
    )
    return port, seen


def test_the_key_maps_back_to_the_drive_item_id() -> None:
    assert item_id_of_key(key_of("01ABC")) == "01ABC"
    wrong_suffix = base64.urlsafe_b64encode(b"a::b::xyz").decode()
    two_parts = base64.urlsafe_b64encode(b"a::b5").decode()
    for bad in ("not base64!", wrong_suffix, two_parts, ""):
        with pytest.raises(ValueError):
            item_id_of_key(bad)


def test_query_uses_the_service_token_only_and_maps_hits() -> None:
    bad_key = "####"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "@odata.count": 3,
                "value": [
                    {"id": key_of("01AAA"), "metadata_spo_item_name": "a.txt", "content": "alpha"},
                    {"id": bad_key, "content": "dropped"},
                    {"id": key_of("01BBB"), "metadata_spo_item_name": "b.txt", "content": None},
                ],
            },
        )

    port, seen = index_port(handler)
    fetched = port.query('"CANARY-X"', 25)
    assert [(h.doc_id, h.name, h.content) for h in fetched.hits] == [
        ("01AAA", "a.txt", "alpha"),
        ("01BBB", "b.txt", ""),
    ]
    assert fetched.unmapped == 1 and fetched.total_match_count == 3
    request = seen[0]
    assert request.url.path == "/indexes/ll-sp-index-b/docs/search"
    assert request.headers["authorization"] == f"Bearer {SERVICE_TOKEN}"
    assert "x-ms-query-source-authorization" not in request.headers  # no user identity here
    body = json.loads(request.content)
    assert body["search"] == '"CANARY-X"' and body["top"] == 25
    assert body["skip"] == 0
    port.query("again", 10, 30)
    assert (
        json.loads(seen[-1].content)["skip"] == 30
    )  # the next page starts after the first 30 hits
    assert "filter" not in body  # the user's text goes into `search` only


def test_top_is_capped() -> None:
    port, seen = index_port(lambda r: httpx.Response(200, json={"value": []}))
    port.query("x", 100000)
    assert json.loads(seen[0].content)["top"] == 1000


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(403),
        httpx.Response(500, text="boom"),
        httpx.Response(200, text="not json"),
        httpx.Response(200, json={"unexpected": True}),
        httpx.Response(200, json=["list"]),
    ],
)
def test_every_bad_answer_is_index_unavailable(response: httpx.Response) -> None:
    port, _ = index_port(lambda r: response)
    with pytest.raises(IndexUnavailable) as err:
        port.query("x", 5)
    assert SERVICE_TOKEN not in str(err.value)


def test_a_transport_error_and_a_failing_token_source_are_index_unavailable() -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    port, _ = index_port(down)
    with pytest.raises(IndexUnavailable):
        port.query("x", 5)
    failing = HttpIndexPort(
        "https://svc",
        client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))),
        token_source=lambda: (_ for _ in ()).throw(RuntimeError("no token")),
    )
    with pytest.raises(IndexUnavailable):
        failing.query("x", 5)


def test_stored_acl_is_read_once_and_cached() -> None:
    now = [1000.0]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"value": [{"id": key_of("01AAA"), "UserIds": ["u1"], "GroupIds": ["g1", "g2"]}]},
        )

    port, seen = index_port(handler, clock=lambda: now[0])
    acl = port.stored_acl("01AAA")
    assert acl is not None and acl.user_ids == ["u1"] and acl.group_ids == ["g1", "g2"]
    assert port.stored_acl("01ZZZ") is None
    assert len(seen) == 1
    now[0] += 31.0
    port.stored_acl("01AAA")
    assert len(seen) == 2


# -- JWKS -----------------------------------------------------------------------------------------


def jwk_of(private: Any, kid: str) -> dict[str, Any]:
    pem = private.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    key = jwt.algorithms.RSAAlgorithm.to_jwk(serialization.load_pem_public_key(pem), as_dict=True)
    return {**key, "kid": kid, "use": "sig"}


def test_jwks_caches_and_throttles_refetching() -> None:
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    calls: list[str] = []
    now = [0.0]

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json={"keys": [jwk_of(private, "k1")]})

    keys = JwksKeys(
        "tenant-1",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        clock=lambda: now[0],
    )
    assert keys.key_for("k1") is not None and keys.key_for("k1") is not None
    assert len(calls) == 1 and "tenant-1/discovery/v2.0/keys" in calls[0]
    assert keys.key_for("unknown") is None  # too soon to refetch
    assert len(calls) == 1
    now[0] += 61.0
    assert keys.key_for("unknown") is None  # allowed to look again, still unknown
    assert len(calls) == 2


def test_a_failed_fetch_keeps_the_known_keys_and_unknown_ids_fail_closed() -> None:
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    answers = [httpx.Response(200, json={"keys": [jwk_of(private, "k1")]}), httpx.Response(500)]
    now = [0.0]
    keys = JwksKeys(
        "t",
        client=httpx.Client(transport=httpx.MockTransport(lambda r: answers.pop(0))),
        clock=lambda: now[0],
    )
    assert keys.key_for("k1") is not None
    now[0] += 100.0
    assert keys.key_for("k2") is None  # the refetch failed
    assert keys.key_for("k1") is not None  # the old keys are still there
