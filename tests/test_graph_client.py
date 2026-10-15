"""Tests for graph_client.py using httpx.MockTransport (no network)."""

import json
import random
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from leaklab.graph_client import (
    GRAPH_BASE_URL,
    GraphClient,
    GraphError,
    parse_retry_after,
)

Handler = Callable[[httpx.Request], httpx.Response]


def make_client(handler: Handler) -> tuple[GraphClient, list[float], list[httpx.Request]]:
    sleeps: list[float] = []
    requests: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    client = GraphClient(
        lambda: "fake-token",
        transport=httpx.MockTransport(recording),
        sleep=sleeps.append,
        rng=random.Random(1),
    )
    return client, sleeps, requests


def error_body(code: str, message: str) -> dict[str, Any]:
    return {"error": {"code": code, "message": message}}


# -- retries ----------------------------------------------------------------------------------


def test_parse_retry_after() -> None:
    assert parse_retry_after("3") == 3.0
    assert parse_retry_after("2.5") == 2.5
    assert parse_retry_after(None) is None
    assert parse_retry_after("garbage") is None
    assert parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT") == 0.0  # date in the past


def test_retry_after_is_honored() -> None:
    calls = iter([httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(200, json={})])
    client, sleeps, requests = make_client(lambda r: next(calls))
    client.get("/users")
    assert sleeps == [7.0]
    assert len(requests) == 2


def test_retry_after_is_capped() -> None:
    calls = iter(
        [httpx.Response(429, headers={"Retry-After": "9999"}), httpx.Response(200, json={})]
    )
    client, sleeps, _ = make_client(lambda r: next(calls))
    client.get("/users")
    assert sleeps == [120.0]


@pytest.mark.parametrize("status", [429, 503, 504])
def test_exponential_backoff_with_jitter_then_success(status: int) -> None:
    calls = iter([httpx.Response(status)] * 3 + [httpx.Response(200, json={"ok": True})])
    client, sleeps, requests = make_client(lambda r: next(calls))
    assert client.get("/users") == {"ok": True}
    assert len(requests) == 4
    for attempt, delay in enumerate(sleeps, start=1):
        base = 2 ** (attempt - 1)
        assert 0.5 * base <= delay <= 1.5 * base


def test_gives_up_after_five_attempts() -> None:
    client, sleeps, requests = make_client(
        lambda r: httpx.Response(503, json=error_body("ServiceUnavailable", "busy"))
    )
    with pytest.raises(GraphError) as exc:
        client.get("/users")
    assert len(requests) == 5
    assert len(sleeps) == 4
    assert exc.value.status_code == 503 and exc.value.code == "ServiceUnavailable"


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 500])
def test_other_errors_are_not_retried(status: int) -> None:
    client, sleeps, requests = make_client(
        lambda r: httpx.Response(status, json=error_body("X", "y"))
    )
    with pytest.raises(GraphError) as exc:
        client.get("/users")
    assert len(requests) == 1 and sleeps == []
    assert exc.value.status_code == status


def test_non_json_error_body() -> None:
    client, _, _ = make_client(lambda r: httpx.Response(500, text="<html>oops</html>"))
    with pytest.raises(GraphError) as exc:
        client.get("/users")
    assert exc.value.code == "non_json_error"


def test_bearer_token_and_base_url() -> None:
    client, _, requests = make_client(lambda r: httpx.Response(200, json={}))
    client.get("/organization")
    assert str(requests[0].url) == f"{GRAPH_BASE_URL}/organization"
    assert requests[0].headers["Authorization"] == "Bearer fake-token"


# -- paging -----------------------------------------------------------------------------------


def test_get_paged_follows_next_link() -> None:
    page2 = f"{GRAPH_BASE_URL}/groups?$skiptoken=abc"

    def handler(request: httpx.Request) -> httpx.Response:
        if "skiptoken" in str(request.url):
            return httpx.Response(200, json={"value": [{"id": "3"}]})
        return httpx.Response(
            200, json={"value": [{"id": "1"}, {"id": "2"}], "@odata.nextLink": page2}
        )

    client, _, requests = make_client(handler)
    assert [i["id"] for i in client.get_paged("/groups")] == ["1", "2", "3"]
    assert len(requests) == 2
    assert requests[1].headers["Authorization"] == "Bearer fake-token"


def test_get_paged_empty() -> None:
    client, _, _ = make_client(lambda r: httpx.Response(200, json={"value": []}))
    assert client.get_paged("/groups") == []


def test_next_link_to_other_host_is_refused() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"value": [], "@odata.nextLink": "https://evil.example/x"})

    client, _, requests = make_client(handler)
    with pytest.raises(GraphError, match="untrusted_url"):
        client.get_paged("/groups")
    assert len(requests) == 1  # the token was never sent to the other host


def test_next_link_loop_is_stopped() -> None:
    link = f"{GRAPH_BASE_URL}/groups?x=1"
    client, _, _ = make_client(
        lambda r: httpx.Response(200, json={"value": [], "@odata.nextLink": link})
    )
    with pytest.raises(GraphError, match="paging_loop"):
        client.get_paged("/groups")


# -- methods ----------------------------------------------------------------------------------


def test_create_user_request_shape_and_no_password_in_errors() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(400, json=error_body("Request_BadRequest", "weak password"))

    client, _, _ = make_client(handler)
    with pytest.raises(GraphError) as exc:
        client.create_user("a@x.com", "A", "a", "S3cret!pw")
    assert seen["body"]["userPrincipalName"] == "a@x.com"
    assert seen["body"]["passwordProfile"]["password"] == "S3cret!pw"
    assert "S3cret!pw" not in str(exc.value)


def test_get_user_by_upn_found_and_missing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "missing" in request.url.path:
            return httpx.Response(404, json=error_body("Request_ResourceNotFound", "nope"))
        return httpx.Response(200, json={"id": "u1"})

    client, _, requests = make_client(handler)
    assert client.get_user_by_upn("here@x.com") == {"id": "u1"}
    assert client.get_user_by_upn("missing@x.com") is None
    assert requests[0].url.path.endswith("/users/here@x.com")


def test_get_user_by_upn_other_errors_propagate() -> None:
    client, _, _ = make_client(lambda r: httpx.Response(403, json=error_body("Forbidden", "no")))
    with pytest.raises(GraphError):
        client.get_user_by_upn("a@x.com")


def test_create_security_group_body() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json={"id": "g1"})

    client, _, _ = make_client(handler)
    assert client.create_security_group("ll-probe-inner", "ll-probe-inner") == {"id": "g1"}
    assert seen["body"]["securityEnabled"] is True
    assert seen["body"]["mailEnabled"] is False
    assert seen["body"]["groupTypes"] == []


def test_find_group_by_name_escapes_quotes_and_handles_counts() -> None:
    results: list[list[dict[str, str]]] = [[], [{"id": "g1"}], [{"id": "g1"}, {"id": "g2"}]]
    filters: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        filters.append(request.url.params["$filter"])
        return httpx.Response(200, json={"value": results.pop(0)})

    client, _, _ = make_client(handler)
    assert client.find_group_by_name("O'Brien") is None
    assert filters[0] == "displayName eq 'O''Brien'"
    assert client.find_group_by_name("x") == {"id": "g1"}
    with pytest.raises(GraphError, match="ambiguous_group"):
        client.find_group_by_name("x")


def test_add_members_use_ref_endpoint() -> None:
    seen: list[tuple[str, dict[str, Any]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, json.loads(request.content)))
        return httpx.Response(204)

    client, _, _ = make_client(handler)
    assert client.add_user_to_group("g1", "u1") is True
    assert client.add_group_to_group("g2", "g1") is True
    assert seen[0] == (
        "/v1.0/groups/g1/members/$ref",
        {"@odata.id": f"{GRAPH_BASE_URL}/directoryObjects/u1"},
    )
    assert seen[1][0] == "/v1.0/groups/g2/members/$ref"
    assert seen[1][1]["@odata.id"].endswith("/directoryObjects/g1")


def test_add_member_already_exists_returns_false() -> None:
    client, _, _ = make_client(
        lambda r: httpx.Response(
            400,
            json=error_body(
                "Request_BadRequest", "One or more added object references already exist"
            ),
        )
    )
    assert client.add_user_to_group("g1", "u1") is False


def test_add_member_other_400_raises() -> None:
    client, _, _ = make_client(
        lambda r: httpx.Response(400, json=error_body("Request_BadRequest", "bad id"))
    )
    with pytest.raises(GraphError):
        client.add_user_to_group("g1", "u1")


def test_list_transitive_member_of_pages() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1.0/users/u1/transitiveMemberOf" or "skip" in str(request.url)
        if "skip" in str(request.url):
            return httpx.Response(200, json={"value": [{"id": "g2"}]})
        return httpx.Response(
            200,
            json={"value": [{"id": "g1"}], "@odata.nextLink": f"{GRAPH_BASE_URL}/x?skip=1"},
        )

    client, _, _ = make_client(handler)
    assert [g["id"] for g in client.list_transitive_member_of("u1")] == ["g1", "g2"]


def test_deletes() -> None:
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        return httpx.Response(204)

    client, _, _ = make_client(handler)
    client.delete_user("u1")
    client.delete_group("g1")
    assert seen == [("DELETE", "/v1.0/users/u1"), ("DELETE", "/v1.0/groups/g1")]
