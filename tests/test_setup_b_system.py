"""The runner's Setup B adapter against the real API app (fake index, fake tenant), in process."""

import httpx
import pytest

from leaklab.runner.model import RunnerError
from leaklab.runner.setup0 import TokenCache
from leaklab.setup_b.system import SetupB
from setup_b_api_world import ALL, Api, P
from setup_b_world import World

PERSONAS = ["p-ceo", "p-intern", "p-plateng"]


def adapter(api: Api, *, allow_debug_client: httpx.Client | None = None) -> SetupB:
    clock = api.world.clock
    return SetupB(
        base_url="http://testserver",
        upns={p: f"{p}@example.test" for p in PERSONAS},
        tokens=TokenCache(
            lambda upn: api.keys.token(api.world.uid(upn.split("@")[0]), upn), clock.time
        ),
        clock=clock.time,
        sleep=clock.sleep,
        http=allow_debug_client or api.client,
    )


def canary(prefix: str) -> str:
    return next(c for c in ALL if c.startswith(prefix))


def make_ready(api: Api) -> None:
    """Two quiet polls of each poller and a verification: what /ready waits for."""
    w = api.world
    for _ in range(3):
        w.sync()
    w.acl.verify_all()


def test_served_and_denied_follow_the_state() -> None:
    api = Api(World(_repo()))
    api.world.sync()
    sut = adapter(api)
    board, general = canary(P + "BRD"), canary(P + "GEN")
    assert sut.read("p-ceo", board).served is True
    denied = sut.read("p-intern", board)
    assert denied.served is False and denied.error is None  # the document IS in the index
    assert sut.read("p-intern", general).served is True


def test_a_document_missing_from_the_index_is_not_a_denial() -> None:
    api = Api(World(_repo()))
    api.world.sync()
    obs = adapter(api).read("p-ceo", "CANARY-NOT-IN-THE-INDEX")
    assert obs.served is None and obs.error == "not_indexed"


def test_without_debug_stats_the_adapter_refuses_to_call_it_a_denial() -> None:
    api = Api(World(_repo()), allow_debug=False)
    api.world.sync()
    sut = adapter(api)
    assert sut.read("p-ceo", canary(P + "BRD")).served is True
    assert sut.read("p-intern", canary(P + "BRD")).error == "no_debug_stats"


def test_http_errors_are_errors_not_denials() -> None:
    api = Api(World(_repo()))
    api.world.sync()
    sut = adapter(api)
    api.index.fail = Exception("boom")  # not IndexUnavailable: the app answers 500
    obs = sut.read("p-ceo", canary(P + "BRD"))
    assert obs.served is None and obs.status == 500
    api.index.fail = None

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    broken = SetupB(
        base_url="http://x",
        upns={"p-ceo": "p-ceo@example.test"},
        tokens=TokenCache(lambda upn: "t", api.world.clock.time),
        clock=api.world.clock.time,
        sleep=api.world.clock.sleep,
        http=httpx.Client(transport=httpx.MockTransport(down)),
    )
    obs = broken.read("p-ceo", "CANARY-X")
    assert obs.served is None and obs.error == "transport"


def test_a_401_drops_the_cached_token() -> None:
    api = Api(World(_repo()))
    api.world.sync()
    fetched: list[str] = []

    def source(upn: str) -> str:
        fetched.append(upn)
        return "not-a-jwt"

    clock = api.world.clock
    sut = SetupB(
        base_url="http://testserver",
        upns={"p-ceo": "p-ceo@example.test"},
        tokens=TokenCache(source, clock.time),
        clock=clock.time,
        sleep=clock.sleep,
        http=api.client,
    )
    assert sut.read("p-ceo", canary(P + "BRD")).status == 401
    sut.read("p-ceo", canary(P + "BRD"))
    assert len(fetched) == 2


def test_prepare_waits_for_a_healthy_api_and_takes_the_first_recall_sample() -> None:
    api = Api(World(_repo(), max_age_s=30.0))
    sut = adapter(api)
    with pytest.raises(RunnerError):  # nothing polled: degraded for ever
        sut.prepare(PERSONAS, ALL)
    api.world.sync()
    make_ready(api)
    sut.prepare(PERSONAS, ALL)
    events = sut.drain_events()
    recall = [e for e in events if e["kind"] == "postfilter_recall"]
    assert len(recall) == len(PERSONAS) * 3
    sample = next(e for e in recall if e["persona"] == "p-ceo" and e["k"] == 3)
    assert {
        "page_size",
        "pages",
        "fetched",
        "allowed_in_window",
        "returned",
        "allowed_total",
        "shortfall",
    } <= set(sample)
    assert sample["shortfall"] == 3 - sample["returned"]
    assert sample["lost_vs_unlimited"] == min(3, sample["allowed_total"]) - sample["returned"]
    health = [e for e in events if e["kind"] == "system_health"]
    assert [h["status"] for h in health] == ["ok"]


def test_health_changes_are_reported_once_each() -> None:
    w = World(_repo(), max_age_s=12.0)
    api = Api(w)
    w.sync()
    sut = adapter(api)
    make_ready(api)
    sut.prepare(PERSONAS, ALL)
    sut.drain_events()
    assert sut.drain_events() == []  # nothing new
    for _ in range(3):
        w.group_src.fail_next = ConnectionError("down")
        w.clock.sleep(5.0)
        w.membership.poll_once()
        w.acl.poll_once()
    events = sut.drain_events()
    assert [e["status"] for e in events if e["kind"] == "system_health"] == ["degraded"]
    assert sut.drain_events() == []


def _repo():  # type: ignore[no-untyped-def]
    from leaklab.setup_b.state.repository import MemoryRepository

    return MemoryRepository()


def test_poller_lag_and_indexer_runs_are_reported_for_the_benchmark() -> None:
    from leaklab.setup_a.client import IndexerRun

    api = Api(World(_repo()))
    api.world.sync()
    runs = [IndexerRun(100.0, 112.0, "success", 45, 0), IndexerRun(400.0, None, "inProgress", 0, 0)]
    clock = api.world.clock
    sut = SetupB(
        base_url="http://testserver",
        upns={p: f"{p}@example.test" for p in PERSONAS},
        tokens=TokenCache(
            lambda upn: api.keys.token(api.world.uid(upn.split("@")[0]), upn), clock.time
        ),
        clock=clock.time,
        sleep=clock.sleep,
        lag_every_s=30.0,
        indexer_runs=lambda: runs,
        http=api.client,
    )
    make_ready(api)
    sut.prepare(PERSONAS, ALL)
    first = sut.drain_events()
    lag = [e for e in first if e["kind"] == "poller_lag"]
    assert (
        len(lag) == 1 and lag[0]["membership_age_s"] is not None and lag[0]["acl_age_s"] is not None
    )
    kinds = [e["kind"] for e in first if e["kind"].startswith("indexer")]
    assert sorted(kinds) == ["indexer_run", "indexer_run_started", "indexer_run_started"]
    done = next(e for e in first if e["kind"] == "indexer_run")
    assert (done["start"], done["end"], done["items_processed"]) == (100.0, 112.0, 45)
    assert [e["kind"] for e in sut.drain_events() if e["kind"] != "postfilter_recall"] == []
    for _ in range(7):  # 35 s pass, the pollers keep running
        api.world.sync()
    runs[1] = IndexerRun(400.0, 409.0, "success", 45, 0)  # the second run ends
    again = sut.drain_events()
    assert [e["kind"] for e in again if e["kind"] != "postfilter_recall"] == [
        "poller_lag",
        "indexer_run",
    ]


def test_a_stale_copy_does_not_make_a_missing_document_look_denied() -> None:
    from leaklab.runner.tenant import resolve_items  # noqa: F401  (documented way to get ids)

    api = Api(World(_repo()))
    api.world.sync()
    board = canary(P + "BRD")
    current = next(h.doc_id for h in api.index.docs if api.index.canary_of[h.doc_id] == board)
    clock = api.world.clock
    sut = SetupB(
        base_url="http://testserver",
        upns={p: f"{p}@example.test" for p in PERSONAS},
        tokens=TokenCache(
            lambda upn: api.keys.token(api.world.uid(upn.split("@")[0]), upn), clock.time
        ),
        clock=clock.time,
        sleep=clock.sleep,
        item_ids={board: current},
        http=api.client,
    )
    assert sut.read("p-ceo", board).served is True
    assert sut.read("p-intern", board).served is False  # the document IS there: a real denial
    # the current document leaves the index; a stale copy with the same text stays
    stale = next(h for h in api.index.docs if h.doc_id == current)
    api.index.docs = [h for h in api.index.docs if h.doc_id != current]
    api.index.docs.append(type(stale)("STALE-ITEM", stale.name, stale.content))
    obs = sut.read("p-ceo", board)
    assert obs.served is None and obs.error == "not_indexed"  # not "denied", not "served"
    # without item ids the adapter cannot tell and falls back to "the phrase matched something"
    plain = adapter(api)
    assert plain.read("p-intern", board).served is False
