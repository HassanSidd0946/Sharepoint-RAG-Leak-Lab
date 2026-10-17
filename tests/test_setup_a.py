"""Setup A (setup_a_basic) against a fake search service: no Azure, no tokens, no network.

The fake search port trims by "persona" the way the real service trims by token: the user token it
receives IS the persona id, and a document comes back only if that persona is allowed on it.
"""

import base64
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from leaklab.runner.model import RunnerError
from leaklab.runner.setup0 import TokenCache
from leaklab.setup_a.client import (
    API,
    DATASOURCE,
    INDEX,
    INDEXER,
    HttpSearchPort,
    IndexerRun,
    parse_run,
    parse_time,
)
from leaklab.setup_a.keys import doc_key, parse_key
from leaklab.setup_a.provision import (
    MIN_SCHEDULE,
    datasource_body,
    ensure_provisioned,
    index_body,
    indexer_body,
)
from leaklab.setup_a.system import SYSTEM_NAME, SetupABasic

C_GEN = "CANARY-GEN-01-aaaaaa"
C_BRD = "CANARY-BRD-01-bbbbbb"
UPNS = {"p-intern": "ll-p-intern@t", "p-ceo": "ll-p-ceo@t"}
CREATED = 1_000.0


class Clock:
    def __init__(self) -> None:
        self.now = 10_000.0

    def time(self) -> float:
        return self.now


def run(
    start: float,
    end: float | None,
    status: str = "success",
    failed: int = 0,
    keys: tuple[str, ...] = (),
) -> IndexerRun:
    return IndexerRun(
        start=start,
        end=end,
        status=status,
        items_processed=43,
        items_failed=failed,
        error_keys=keys,
    )


class FakeSearch:
    """Documents with per-persona access; indexer runs set by the test; failures injectable."""

    def __init__(self) -> None:
        self.docs: dict[str, set[str]] = {C_GEN: {"p-intern"}, C_BRD: {"p-ceo"}}
        self.runs: list[IndexerRun] = []
        self.status = 200
        self.answer: Any = None
        self.queries: list[tuple[str, str]] = []
        self.run_polls = 0
        self.indexed: set[str] = set(self.docs)  # what the index holds

    def query(self, user_token: str, phrase: str) -> tuple[int, list[dict[str, Any]] | str]:
        self.queries.append((user_token, phrase))
        if self.status != 200:
            return self.status, self.answer or "error"
        wanted = phrase.strip('"')
        return 200, [
            {"metadata_spo_item_name": "x.txt", "content": f"Reference code: {c}\n"}
            for c, who in self.docs.items()
            if c in self.indexed and user_token in who and wanted in c
        ]

    def indexer_runs(self) -> list[IndexerRun]:
        self.run_polls += 1
        return list(self.runs)


def make(
    search: FakeSearch,
    clock: Clock | None = None,
    source: Callable[[str], str] | None = None,
    created: dict[str, float] | None = None,
) -> tuple[SetupABasic, Clock, list[str]]:
    clock = clock or Clock()
    fetched: list[str] = []

    def default_source(upn: str) -> str:
        fetched.append(upn)
        return next(p for p, u in UPNS.items() if u == upn)  # the "token" is the persona id

    tokens = TokenCache(source or default_source, clock.time, max_age_s=120.0)
    system = SetupABasic(
        search=search,
        upns=UPNS,
        tokens=tokens,
        created_at={C_GEN: CREATED, C_BRD: CREATED} if created is None else created,
        clock=clock.time,
        runs_poll_s=15.0,
    )
    return system, clock, fetched


# -- reads ---------------------------------------------------------------------------------------


def test_the_label_is_setup_a_basic_everywhere() -> None:
    system, _, _ = make(FakeSearch())
    assert system.name == SYSTEM_NAME == "setup_a_basic"
    assert system.read("p-intern", C_GEN).system == "setup_a_basic"


def test_a_persona_who_may_read_gets_served_true_and_the_query_is_an_exact_phrase() -> None:
    search = FakeSearch()
    system, _, _ = make(search)
    obs = system.read("p-intern", C_GEN)
    assert obs.served is True and obs.status == 200 and obs.error is None
    assert search.queries == [("p-intern", f'"{C_GEN}"')]


def test_content_with_a_different_canary_does_not_count_as_served() -> None:
    search = FakeSearch()
    system, _, _ = make(search)
    search.query = lambda token, phrase: (200, [{"content": "CANARY-GEN-01-zzzzzz"}])  # type: ignore[method-assign]
    search.runs = [run(CREATED + 10, CREATED + 20)]
    assert system.read("p-intern", C_GEN).served is False  # present, but not THIS canary


def test_an_empty_answer_is_a_denial_only_once_the_document_is_known_to_be_indexed() -> None:
    search = FakeSearch()
    search.runs = [
        run(CREATED + 10, CREATED + 40)
    ]  # a clean run that started after the document existed
    system, _, _ = make(search)
    obs = system.read("p-intern", C_BRD)  # p-intern may not read Board
    assert obs.served is False and obs.status == 200 and obs.error is None


def test_before_any_run_has_seen_the_document_the_read_is_not_indexed_never_false() -> None:
    search = FakeSearch()
    search.indexed = set()  # nothing indexed yet
    search.runs = [
        run(CREATED - 50, CREATED - 10)
    ]  # the last run STARTED before the document existed
    system, _, _ = make(search)
    obs = system.read("p-intern", C_GEN)
    assert obs.served is None and obs.error == "not_indexed" and obs.status == 200


@pytest.mark.parametrize(
    "runs",
    [
        [],
        [run(CREATED + 5, None, "inProgress")],
        [run(CREATED + 5, CREATED + 9, "transientFailure")],
        [run(CREATED + 5, CREATED + 9, "success", failed=1)],
        [run(CREATED + 5, CREATED + 9, "success", failed=0, keys=("k1",))],
    ],
)
def test_runs_that_prove_nothing_keep_it_not_indexed(runs: list[IndexerRun]) -> None:
    search = FakeSearch()
    search.runs = runs
    system, _, _ = make(search)
    system._keys = {C_BRD: "k1"}  # noqa: SLF001
    obs = system.read("p-intern", C_BRD)
    assert obs.served is None and obs.error == "not_indexed"


def test_a_run_that_names_the_document_in_an_error_is_not_evidence_even_if_successful() -> None:
    search = FakeSearch()
    search.runs = [run(CREATED + 5, CREATED + 9, "success", failed=0, keys=("kb",))]
    system, _, _ = make(search)
    system._keys = {C_BRD: "kb"}  # noqa: SLF001
    assert system.read("p-intern", C_BRD).error == "not_indexed"


def test_presence_is_remembered_once_any_persona_has_been_served_the_document() -> None:
    search = FakeSearch()
    search.runs = []  # no indexer evidence at all
    system, _, _ = make(search)
    assert system.read("p-ceo", C_BRD).served is True  # the entitled persona proves it is there
    obs = system.read("p-intern", C_BRD)  # now an empty answer for someone else is a real denial
    assert obs.served is False and obs.error is None


def test_an_unknown_creation_time_means_presence_unknown_not_denied() -> None:
    search = FakeSearch()
    search.runs = [run(CREATED + 5, CREATED + 9)]
    system, _, _ = make(search, created={})
    obs = system.read("p-intern", C_BRD)
    assert obs.served is None and obs.error == "presence_unknown"


@pytest.mark.parametrize("status", [401, 403, 429, 500, 503])
def test_service_errors_are_errors_never_denials(status: int) -> None:
    search = FakeSearch()
    search.status = status
    system, _, _ = make(search)
    obs = system.read("p-intern", C_GEN)
    assert obs.served is None and obs.error == f"http:{status}" and obs.status == status


def test_a_transport_failure_is_an_error() -> None:
    search = FakeSearch()
    search.status, search.answer = 0, "transport:ConnectTimeout"
    system, _, _ = make(search)
    obs = system.read("p-intern", C_GEN)
    assert obs.served is None and obs.error == "transport:ConnectTimeout"


def test_a_401_drops_the_token_so_the_next_read_signs_in_again() -> None:
    search = FakeSearch()
    system, _, fetched = make(search)
    search.status = 401
    system.read("p-intern", C_GEN)
    search.status = 200
    system.read("p-intern", C_GEN)
    assert fetched.count("ll-p-intern@t") == 2


def test_persona_tokens_are_reused_for_120_seconds_then_refreshed() -> None:
    search = FakeSearch()
    system, clock, fetched = make(search)
    for _ in range(5):
        system.read("p-intern", C_GEN)
        clock.now += 20
    assert fetched == ["ll-p-intern@t"]  # 100 s: one sign-in
    clock.now += 30  # 130 s old
    system.read("p-intern", C_GEN)
    assert fetched == ["ll-p-intern@t"] * 2


def test_a_failed_sign_in_is_an_error_read_and_is_not_retried_every_time() -> None:
    search = FakeSearch()
    attempts: list[str] = []

    def refuse(upn: str) -> str:
        attempts.append(upn)
        raise PermissionError("AADSTS50196")

    system, clock, _ = make(search, source=refuse)
    first = system.read("p-intern", C_GEN)
    assert first.served is None and first.error == "token:PermissionError"
    for _ in range(4):
        clock.now += 2
        assert system.read("p-intern", C_GEN).served is None
    assert len(attempts) == 1
    assert search.queries == []  # no query was sent without a token


def test_prepare_signs_in_checks_the_service_and_fails_loudly() -> None:
    search = FakeSearch()
    system, _, fetched = make(search)
    system.prepare(["p-intern", "p-ceo"], [C_GEN])
    assert sorted(fetched) == ["ll-p-ceo@t", "ll-p-intern@t"]
    search.status, search.answer = 403, "forbidden"
    with pytest.raises(RunnerError, match="HTTP 403"):
        system.prepare(["p-intern"], [C_GEN])


# -- indexer runs reported to the journal --------------------------------------------------------


def test_each_indexer_run_is_reported_once_when_it_starts_and_once_when_it_ends() -> None:
    search = FakeSearch()
    system, clock, _ = make(search)
    search.runs = [run(100.0, None, "inProgress")]
    assert system.drain_events() == [{"kind": "indexer_run_started", "start": 100.0}]
    assert system.drain_events() == []  # nothing new (also: the poll is rate limited)
    clock.now += 20
    search.runs = [run(100.0, 118.5, "success")]
    done = system.drain_events()
    assert [e["kind"] for e in done] == ["indexer_run"]
    assert done[0] | {"end": 118.5} == {
        "kind": "indexer_run",
        "start": 100.0,
        "end": 118.5,
        "status": "success",
        "items_processed": 43,
        "items_failed": 0,
        "error": None,
    }
    clock.now += 20
    assert system.drain_events() == []  # never twice


def test_the_run_status_is_polled_at_most_every_15_seconds() -> None:
    search = FakeSearch()
    system, clock, _ = make(search)
    search.runs = [run(100.0, 110.0)]
    system.drain_events()
    before = search.run_polls
    for _ in range(5):
        clock.now += 2
        system.drain_events()
    assert search.run_polls == before
    clock.now += 10
    system.drain_events()
    assert search.run_polls == before + 1


# -- keys, parsing, the real HTTP port ------------------------------------------------------------


def test_a_document_key_round_trips_and_carries_the_indexers_extra_character() -> None:
    key = doc_key("host,aaa,bbb", "b!drive", "01ITEM")
    assert parse_key(key) == ("host,aaa,bbb", "b!drive", "01ITEM5")  # what the index stores
    with pytest.raises(ValueError, match="not a SharePoint"):
        parse_key(base64.urlsafe_b64encode(b"just text").decode())


def test_times_and_runs_are_parsed_from_the_services_status() -> None:
    assert parse_time("2026-10-17T08:28:19.056Z") == pytest.approx(1792225699.056, abs=1.0)
    assert parse_time(None) is None
    r = parse_run(
        {
            "startTime": "2026-10-17T08:28:19.056Z",
            "endTime": "2026-10-17T08:28:22.423Z",
            "status": "success",
            "itemsProcessed": 2,
            "itemsFailed": 0,
            "errors": [{"key": "abc", "errorMessage": "x"}],
        }
    )
    assert r.finished and r.items_processed == 2 and r.error_keys == ("abc",)
    running = parse_run({"startTime": "2026-10-17T08:28:19Z", "status": "inProgress"})
    assert not running.finished


def mock_port(handler: Callable[[httpx.Request], httpx.Response]) -> HttpSearchPort:
    return HttpSearchPort(
        "https://s.search.windows.net/",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        token_source=lambda: "driver-token",
    )


def test_the_http_port_sends_the_driver_bearer_and_the_user_token_separately() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"value": [{"content": "x"}]})

    status, docs = mock_port(handler).query("user-token", '"CANARY"')
    assert (status, docs) == (200, [{"content": "x"}])
    req = seen[0]
    assert req.url.path == f"/indexes/{INDEX}/docs/search" and req.url.params["api-version"] == API
    assert req.headers["Authorization"] == "Bearer driver-token"
    assert req.headers["x-ms-query-source-authorization"] == "user-token"
    assert b'"search":"\\"CANARY\\""' in req.content.replace(b" ", b"")


def test_the_http_port_turns_failures_into_statuses_never_exceptions() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("handshake")

    assert mock_port(boom).query("t", "p") == (0, "transport:ConnectTimeout")
    assert mock_port(lambda r: httpx.Response(503, text="busy")).query("t", "p")[0] == 503

    def bad_token() -> str:
        raise RuntimeError("no certificate")

    port = HttpSearchPort(
        "https://s",
        client=httpx.Client(transport=httpx.MockTransport(boom)),
        token_source=bad_token,
    )
    assert port.query("t", "p") == (0, "driver-token:RuntimeError")


def test_the_http_port_reads_indexer_runs_including_the_last_result() -> None:
    body = {
        "lastResult": {
            "startTime": "2026-10-17T08:30:00Z",
            "endTime": "2026-10-17T08:30:05Z",
            "status": "success",
            "itemsProcessed": 3,
        },
        "executionHistory": [
            {
                "startTime": "2026-10-17T08:20:00Z",
                "endTime": "2026-10-17T08:20:09Z",
                "status": "success",
                "itemsProcessed": 43,
            }
        ],
    }
    port = mock_port(lambda r: httpx.Response(200, json=body))
    runs = port.indexer_runs()
    assert [r.items_processed for r in runs] == [3, 43]  # the last result is not missing
    assert mock_port(lambda r: httpx.Response(500)).indexer_runs() == []


# -- provisioning ------------------------------------------------------------------------------


def test_the_bodies_follow_the_docs_and_the_probe_results() -> None:
    ds = datasource_body(
        site_url="https://t.sharepoint.com/sites/L",
        app_id="a",
        secret="SECRET",
        tenant_id="t",
        folder_url="https://t.sharepoint.com/sites/L/Shared Documents/ll-lab",
    )
    assert ds["type"] == "sharepoint" and ds["indexerPermissionOptions"] == ["userIds", "groupIds"]
    assert ds["container"] == {
        "name": "useQuery",
        "query": "includeFolder=https://t.sharepoint.com/sites/L/Shared Documents/ll-lab",
    }
    assert "ApplicationSecret=SECRET" in ds["credentials"]["connectionString"]
    with pytest.raises(ValueError, match="https"):
        datasource_body(
            site_url="s", app_id="a", secret="x", tenant_id="t", folder_url="t.sharepoint.com/x"
        )
    idx = index_body()
    assert idx["permissionFilterOption"] == "enabled"
    acl = {f["name"]: f.get("permissionFilter") for f in idx["fields"]}
    assert acl["UserIds"] == "userIds" and acl["GroupIds"] == "groupIds"
    ixr = indexer_body()
    assert ixr["schedule"] == {"interval": "PT5M"} == {"interval": MIN_SCHEDULE}
    assert {m["targetFieldName"] for m in ixr["fieldMappings"]} == {"id", "UserIds", "GroupIds"}
    # nothing about SharePoint groups: that is what "basic" means
    assert "sharePointConnectorAppRegistration" not in idx
    assert all("sharepointSiteUrl" not in f for f in idx["fields"])


class FakeService:
    """Just enough of the service for ensure_provisioned: stores objects, answers GET and PUT."""

    def __init__(self) -> None:
        self.objects: dict[str, dict[str, Any]] = {}
        self.puts: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        assert (
            request.headers["Authorization"] == "Bearer b"
            and request.url.params["api-version"] == API
        )
        if request.method == "GET":
            found = self.objects.get(path)
            return httpx.Response(200, json=found) if found is not None else httpx.Response(404)
        import json

        self.objects[path] = json.loads(request.content)
        self.puts.append(path)
        return httpx.Response(201, json={})


def provision(service: FakeService, body: dict[str, Any] | None = None) -> dict[str, str]:
    http = httpx.Client(
        transport=httpx.MockTransport(service), base_url="https://s.search.windows.net"
    )
    return ensure_provisioned(
        http, "https://s.search.windows.net", "b", {"x": 1} if body is None else body
    )


def test_provisioning_creates_everything_once_and_then_changes_nothing() -> None:
    service = FakeService()
    assert provision(service) == {
        "data source": "created",
        "index": "created",
        "indexer": "created",
    }
    assert service.objects[f"/indexers/{INDEXER}"]["schedule"] == {"interval": "PT5M"}
    puts = len(service.puts)
    assert provision(service) == {
        "data source": "unchanged",
        "index": "unchanged",
        "indexer": "unchanged",
    }
    assert len(service.puts) == puts


def test_a_wrong_schedule_is_corrected_and_nothing_else_is_touched() -> None:
    service = FakeService()
    provision(service)
    service.objects[f"/indexers/{INDEXER}"]["schedule"] = {"interval": "PT1H"}
    service.puts.clear()
    result = provision(service)
    assert result["indexer"].startswith("updated") and service.puts == [f"/indexers/{INDEXER}"]
    assert service.objects[f"/indexers/{INDEXER}"]["schedule"] == {"interval": "PT5M"}


def test_a_missing_data_source_without_a_secret_is_an_error_not_an_empty_object() -> None:
    with pytest.raises(RuntimeError, match="no ingestion app secret"):
        provision(FakeService(), body={})
    assert DATASOURCE and INDEX  # names are shared constants
