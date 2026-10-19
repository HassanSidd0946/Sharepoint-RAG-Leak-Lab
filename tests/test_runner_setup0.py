"""Setup0SharePoint against a mocked Graph (httpx.MockTransport): no network, no real tokens.

What is asserted is the read contract from probes/consistency_probe.py: Graph 302 -> download
without the Authorization header (only on *.sharepoint.com) -> served = canary in body; 403/404 =
denied; anything else = an ERROR (served None), never a denial.
"""

import base64
import json
from collections.abc import Callable

import httpx
import pytest

from leaklab.runner.model import RunnerError
from leaklab.runner.setup0 import Setup0SharePoint, TokenCache

CANARY = "CANARY-S1D-01-b67fb8"
DRIVE, ITEM = "drive1", "item1"
UPN = "ll-p-plateng@contoso.onmicrosoft.com"
GRAPH_URL = f"https://graph.microsoft.com/v1.0/drives/{DRIVE}/items/{ITEM}/content"
DOWNLOAD = "https://contoso.sharepoint.com/_layouts/download.aspx?sig=abc"


def jwt(exp: float) -> str:
    def b64(obj: object) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    return f"{b64({'alg': 'none'})}.{b64({'exp': exp, 'upn': UPN})}.sig"


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def time(self) -> float:
        return self.now


def make(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    clock: Clock | None = None,
    source: Callable[[str], str] | None = None,
) -> tuple[Setup0SharePoint, TokenCache, Clock]:
    clock = clock or Clock()
    tokens = TokenCache(source or (lambda upn: jwt(clock.now + 3600)), clock.time)
    system = Setup0SharePoint(
        drive_id=DRIVE,
        item_ids={CANARY: ITEM},
        upns={"p-plateng": UPN},
        tokens=tokens,
        clock=clock.time,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    return system, tokens, clock


def graph_302(request: httpx.Request) -> httpx.Response:
    assert str(request.url) == GRAPH_URL
    assert request.headers["Authorization"].startswith("Bearer ")
    return httpx.Response(302, headers={"Location": DOWNLOAD})


def test_allowed_read_follows_the_redirect_without_the_token_and_matches_the_canary() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host == "graph.microsoft.com":
            return graph_302(request)
        assert "Authorization" not in request.headers  # the pre-authenticated URL gets no token
        return httpx.Response(200, text=f"Reference code: {CANARY}\n")

    system, _, _ = make(handler)
    obs = system.read("p-plateng", CANARY)
    assert obs.served is True and obs.status == 302 and obs.final_status == 200
    assert obs.error is None and obs.system == "setup0" and obs.latency_ms is not None
    assert len(seen) == 2


@pytest.mark.parametrize("status", [403, 404])
def test_graph_refusing_is_a_denial(status: int) -> None:
    system, _, _ = make(lambda r: httpx.Response(status, json={"error": {"code": "accessDenied"}}))
    obs = system.read("p-plateng", CANARY)
    assert obs.served is False and obs.status == status and obs.error is None


def test_a_refused_download_is_a_denial() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "graph.microsoft.com":
            return graph_302(request)
        return httpx.Response(403)

    system, _, _ = make(handler)
    obs = system.read("p-plateng", CANARY)
    assert obs.served is False and obs.final_status == 403


def test_content_without_the_canary_is_not_served() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "graph.microsoft.com":
            return graph_302(request)
        return httpx.Response(200, text="some other document")

    system, _, _ = make(handler)
    assert system.read("p-plateng", CANARY).served is False


def test_a_redirect_to_an_unexpected_host_is_an_error_and_is_not_followed() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        return httpx.Response(302, headers={"Location": "https://evil.example.com/x"})

    system, _, _ = make(handler)
    obs = system.read("p-plateng", CANARY)
    assert obs.served is None and "unexpected host" in (obs.error or "")
    assert calls == ["graph.microsoft.com"]


@pytest.mark.parametrize("status", [401, 429, 500, 502, 503, 400])
def test_throttling_and_server_errors_are_errors_not_denials(status: int) -> None:
    system, _, _ = make(lambda r: httpx.Response(status))
    obs = system.read("p-plateng", CANARY)
    assert obs.served is None and obs.error is not None and obs.status == status


def test_a_failing_download_is_an_error_not_a_denial() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "graph.microsoft.com":
            return graph_302(request)
        return httpx.Response(503)

    system, _, _ = make(handler)
    obs = system.read("p-plateng", CANARY)
    assert obs.served is None and obs.final_status == 503


def test_a_transport_error_is_an_error_and_read_never_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("handshake timed out")

    system, _, _ = make(handler)
    obs = system.read("p-plateng", CANARY)
    assert obs.served is None and obs.error == "transport:ConnectTimeout"


def test_an_unknown_canary_or_persona_is_an_error() -> None:
    system, _, _ = make(graph_302)
    assert system.read("p-plateng", "CANARY-NOPE").error == "unknown canary"
    assert system.read("p-nobody", CANARY).served is None


def test_prepare_signs_everyone_in_and_checks_the_canaries() -> None:
    system, tokens, _ = make(graph_302)
    system.prepare(["p-plateng"], [CANARY])
    assert tokens.fetches == 1
    with pytest.raises(RunnerError, match="no drive item"):
        system.prepare(["p-plateng"], [CANARY, "CANARY-MISSING"])


def test_prepare_raises_when_a_sign_in_fails() -> None:
    def refuse(upn: str) -> str:
        raise PermissionError("bad password")

    system, _, _ = make(graph_302, source=refuse)
    with pytest.raises(PermissionError):
        system.prepare(["p-plateng"], [CANARY])


# -- tokens ----------------------------------------------------------------------------------------


def test_a_token_is_reused_until_shortly_before_it_expires_then_renewed() -> None:
    clock = Clock()
    issued: list[float] = []

    def source(upn: str) -> str:
        issued.append(clock.now)
        return jwt(clock.now + 1000)

    system, tokens, _ = make(lambda r: httpx.Response(404), clock=clock, source=source)
    system.read("p-plateng", CANARY)
    clock.now += 600  # 400 s of life left: more than the 300 s margin
    system.read("p-plateng", CANARY)
    assert len(issued) == 1
    clock.now += 150  # 250 s left: inside the margin, renew before it can expire mid-read
    system.read("p-plateng", CANARY)
    assert len(issued) == 2 and tokens.fetches == 2


def test_a_401_drops_the_token_so_the_next_read_signs_in_again() -> None:
    clock = Clock()
    fetched: list[str] = []

    def source(upn: str) -> str:
        fetched.append(upn)
        return jwt(clock.now + 3600)

    system, _, _ = make(lambda r: httpx.Response(401), clock=clock, source=source)
    system.read("p-plateng", CANARY)
    system.read("p-plateng", CANARY)
    assert len(fetched) == 2


def test_after_a_failed_sign_in_the_persona_is_not_retried_every_read() -> None:
    clock = Clock()
    attempts: list[float] = []

    def source(upn: str) -> str:
        attempts.append(clock.now)
        raise ConnectionError("entra unreachable")

    system, _, _ = make(graph_302, clock=clock, source=source)
    first = system.read("p-plateng", CANARY)
    assert first.served is None and first.error == "token:ConnectionError"
    for _ in range(5):
        clock.now += 2
        assert system.read("p-plateng", CANARY).served is None
    assert len(attempts) == 1  # backed off for 30 s
    clock.now += 40
    system.read("p-plateng", CANARY)
    assert len(attempts) == 2


def test_a_token_without_an_expiry_claim_is_renewed_often_not_trusted_forever() -> None:
    clock = Clock()
    fetched: list[int] = []

    def source(upn: str) -> str:
        fetched.append(1)
        return "not-a-jwt"

    system, _, _ = make(lambda r: httpx.Response(404), clock=clock, source=source)
    system.read("p-plateng", CANARY)
    clock.now += 400
    system.read("p-plateng", CANARY)  # assumed 600 s life minus the 300 s margin
    assert len(fetched) == 2
