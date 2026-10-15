"""Tests for sp_rest.py using httpx.MockTransport (no network)."""

import json
import random
from collections.abc import Callable

import httpx
import pytest

from leaklab.sp_rest import (
    SharePointRest,
    SPRestError,
    file_item_path,
    folder_item_path,
    parse_body,
)

SITE = "https://t.sharepoint.com/sites/LeakLab"
Handler = Callable[[httpx.Request], httpx.Response]


def make_client(handler: Handler) -> tuple[SharePointRest, list[float], list[httpx.Request]]:
    sleeps: list[float] = []
    requests: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    client = SharePointRest(
        SITE,
        lambda: "fake-token",
        transport=httpx.MockTransport(recording),
        sleep=sleeps.append,
        rng=random.Random(1),
    )
    return client, sleeps, requests


def ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"value": []})


def test_item_paths_encode_spaces_and_quotes() -> None:
    url = "/sites/LeakLab/Shared Documents/it's here"
    assert folder_item_path(url) == (
        "/web/GetFolderByServerRelativeUrl('/sites/LeakLab/Shared%20Documents/it%27%27s%20here')"
        "/ListItemAllFields"
    )
    assert file_item_path("/a/b.txt") == (
        "/web/GetFileByServerRelativeUrl('/a/b.txt')/ListItemAllFields"
    )


def test_site_url_must_be_https() -> None:
    with pytest.raises(ValueError):
        SharePointRest("http://t.sharepoint.com/sites/x", lambda: "t")
    with pytest.raises(ValueError):
        SharePointRest("not a url", lambda: "t")


def test_path_must_start_with_slash() -> None:
    client, _, _ = make_client(ok)
    with pytest.raises(ValueError):
        client.request("GET", "web")


def test_headers_and_url() -> None:
    client, _, requests = make_client(ok)
    client.request("GET", "/web/roledefinitions")
    req = requests[0]
    assert str(req.url) == f"{SITE}/_api/web/roledefinitions"
    assert req.headers["Authorization"] == "Bearer fake-token"
    assert req.headers["Accept"] == "application/json;odata=nometadata"
    assert "Content-Type" not in req.headers


def test_post_body_sets_content_type() -> None:
    client, _, requests = make_client(ok)
    client.ensure_user("c:0t.c|tenant|abc")
    req = requests[0]
    assert req.method == "POST"
    assert req.url.path.endswith("/_api/web/ensureuser")
    assert req.headers["Content-Type"] == "application/json;odata=nometadata"
    assert json.loads(req.content) == {"logonName": "c:0t.c|tenant|abc"}


def test_retry_after_honored_then_success() -> None:
    calls = iter([httpx.Response(429, headers={"Retry-After": "4"}), httpx.Response(200, json={})])
    client, sleeps, requests = make_client(lambda r: next(calls))
    client.request("GET", "/web")
    assert sleeps == [4.0]
    assert len(requests) == 2


def test_gives_up_after_five_attempts_with_error_body() -> None:
    client, sleeps, requests = make_client(
        lambda r: httpx.Response(503, json={"error": {"code": "busy"}})
    )
    with pytest.raises(SPRestError) as exc:
        client.request("GET", "/web")
    assert len(requests) == 5 and len(sleeps) == 4
    assert exc.value.status_code == 503
    assert exc.value.body == {"error": {"code": "busy"}}


@pytest.mark.parametrize("status", [400, 401, 403, 404, 500])
def test_other_errors_not_retried(status: int) -> None:
    client, sleeps, requests = make_client(lambda r: httpx.Response(status, text="plain text"))
    with pytest.raises(SPRestError) as exc:
        client.request("GET", "/web")
    assert len(requests) == 1 and sleeps == []
    assert exc.value.body == "plain text"


def test_parse_body_empty_and_json() -> None:
    assert parse_body(httpx.Response(200)) is None
    assert parse_body(httpx.Response(200, json={"a": 1})) == {"a": 1}


def test_break_role_inheritance_path() -> None:
    client, _, requests = make_client(ok)
    item = folder_item_path("/sites/x/Docs/f")
    client.break_role_inheritance(item, copy_role_assignments=False, clear_subscopes=True)
    req = requests[0]
    assert req.method == "POST"
    assert req.url.path.endswith(
        "/ListItemAllFields/breakroleinheritance(copyRoleAssignments=false,clearSubscopes=true)"
    )


def test_add_role_assignment_path() -> None:
    client, _, requests = make_client(ok)
    client.add_role_assignment("/web/x", 12, 1073741826)
    assert requests[0].url.path.endswith(
        "/web/x/roleassignments/addroleassignment(principalid=12,roledefid=1073741826)"
    )


def test_read_calls_use_expected_endpoints_and_params() -> None:
    client, _, requests = make_client(ok)
    client.get_unique_flag("/web/x")
    client.get_role_definitions()
    client.get_role_assignments("/web/x")
    client.get_associated_group("owner")
    client.get_group_users(7)
    paths = [r.url.path.removeprefix("/sites/LeakLab/_api") for r in requests]
    assert paths == [
        "/web/x",
        "/web/roledefinitions",
        "/web/x/roleassignments",
        "/web/associatedownergroup",
        "/web/sitegroups/getbyid(7)/users",
    ]
    assert requests[0].url.params["$select"] == "HasUniqueRoleAssignments"
    assert requests[2].url.params["$expand"] == "Member,RoleDefinitionBindings"


def test_token_never_in_error() -> None:
    client, _, _ = make_client(lambda r: httpx.Response(403, json={"error": "denied"}))
    with pytest.raises(SPRestError) as exc:
        client.request("GET", "/web")
    assert "fake-token" not in str(exc.value) and "fake-token" not in repr(exc.value.body)


def test_remove_role_assignment_path() -> None:
    client, _, requests = make_client(ok)
    client.remove_role_assignment("/web", 12, 1073741826)
    assert requests[0].method == "POST"
    assert requests[0].url.path.endswith(
        "/web/roleassignments/removeroleassignment(principalid=12,roledefid=1073741826)"
    )
