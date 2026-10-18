"""/ready: two quiet polls per poller and one completed verification; the runner waits for it;
and the start-up log that compares the first sync with the state at readiness."""

from typing import Any

import pytest

from leaklab.runner.setup0 import TokenCache
from leaklab.setup_b.state.readiness import Readiness, StartupLog, snapshot
from leaklab.setup_b.state.repository import MemoryRepository
from leaklab.setup_b.system import SetupB
from setup_b_api_world import ALL, Api, P
from setup_b_world import World

PERSONAS = ["p-ceo", "p-intern", "p-plateng"]


# -- the tracker -------------------------------------------------------------------------------------


def quiet(r: Readiness, n: int = 2) -> None:
    for _ in range(n):
        r.record_poll("membership", ok=True, changed=False)
        r.record_poll("acl", ok=True, changed=False)


def test_ready_needs_two_quiet_polls_of_each_poller_and_one_verification() -> None:
    r = Readiness()
    assert not r.state().ready
    r.record_verification(ok=True, changed=False)
    assert not r.state().ready  # a verification alone is not enough
    quiet(r, 1)
    assert not r.state().ready
    r.record_poll("membership", ok=True, changed=False)
    assert not r.state().ready  # the ACL poller has had only one quiet poll
    r.record_poll("acl", ok=True, changed=False)
    assert r.state().ready
    assert r.state().quiet_polls == {"membership": 2, "acl": 2}


def test_no_verification_no_ready_however_quiet_the_polls_are() -> None:
    r = Readiness()
    quiet(r, 10)
    assert not r.state().ready
    r.record_verification(ok=False, changed=False)  # a failed verification does not count
    assert not r.state().ready
    r.record_verification(ok=True, changed=False)
    assert r.state().ready


@pytest.mark.parametrize(("ok", "changed"), [(False, False), (True, True), (False, True)])
def test_a_failed_or_changing_poll_resets_that_pollers_streak(ok: bool, changed: bool) -> None:
    r = Readiness()
    r.record_verification(ok=True, changed=False)
    quiet(r, 5)
    assert r.state().ready
    r.record_poll("membership", ok=ok, changed=changed)
    assert not r.state().ready and r.state().quiet_polls["membership"] == 0
    assert r.state().quiet_polls["acl"] == 5  # the other poller is untouched
    quiet(r, 2)
    assert r.state().ready


def test_a_verification_that_found_drift_changed_the_state_so_the_acl_streak_restarts() -> None:
    r = Readiness()
    quiet(r, 4)
    r.record_verification(ok=True, changed=True)
    assert not r.state().ready and r.state().verifications == 1
    r.record_poll("acl", ok=True, changed=False)
    r.record_poll("acl", ok=True, changed=False)
    assert r.state().ready


def test_the_state_is_reported_as_plain_data() -> None:
    state = Readiness(needed_quiet_polls=3).state().as_dict()
    assert state == {
        "ready": False,
        "quiet_polls": {"membership": 0, "acl": 0},
        "verifications": 0,
        "needed_quiet_polls": 3,
    }


# -- wired into the stores -----------------------------------------------------------------------------


def test_the_stores_feed_the_tracker_and_a_change_makes_it_unready_again() -> None:
    w = World(MemoryRepository())
    w.sync()  # the first sync changes everything
    assert not w.readiness.state().ready and w.readiness.state().quiet_polls == {
        "membership": 0,
        "acl": 0,
    }
    w.sync()
    w.sync()
    assert w.readiness.state().quiet_polls == {"membership": 2, "acl": 2}
    assert not w.readiness.state().ready  # no verification yet
    w.acl.verify_all()
    assert w.readiness.state().ready
    w.apply("grant_removed", target="S1-Direct", principal_kind="user", principal_ref="p-plateng")
    w.sync()
    assert not w.readiness.state().ready  # the delta found a change
    w.sync()
    w.sync()
    assert w.readiness.state().ready


def test_a_failing_source_resets_the_streak() -> None:
    w = World(MemoryRepository())
    for _ in range(3):
        w.sync()
    w.acl.verify_all()
    assert w.readiness.state().ready
    w.group_src.fail_next = ConnectionError("down")
    w.sync()
    assert not w.readiness.state().ready


# -- the endpoint ---------------------------------------------------------------------------------------


def test_ready_is_503_until_the_state_converged_and_queries_are_not_gated_by_it() -> None:
    api = Api(World(MemoryRepository()))
    api.world.sync()
    resp = api.client.get("/ready")
    assert resp.status_code == 503 and resp.json()["ready"] is False
    assert resp.json()["quiet_polls"] == {"membership": 0, "acl": 0}
    assert api.served_canaries("p-ceo")  # /ready is advisory: the fail-closed rules decide answers
    api.world.sync()
    api.world.sync()
    api.world.acl.verify_all()
    ok = api.client.get("/ready")
    assert ok.status_code == 200 and ok.json()["ready"] is True and ok.json()["verifications"] == 1


def test_without_a_tracker_the_service_says_ready() -> None:
    api = Api(World(MemoryRepository()))
    api.service._readiness = None  # noqa: SLF001
    body = api.client.get("/ready").json()
    assert body == {"ready": True, "tracked": False}


# -- the runner waits for it -----------------------------------------------------------------------------


def adapter(api: Api, **kw: Any) -> SetupB:
    clock = api.world.clock
    return SetupB(
        base_url="http://testserver",
        upns={p: f"{p}@example.test" for p in PERSONAS},
        tokens=TokenCache(
            lambda upn: api.keys.token(api.world.uid(upn.split("@")[0]), upn), clock.time
        ),
        clock=clock.time,
        sleep=kw.pop("sleep", clock.sleep),
        http=api.client,
        **kw,
    )


def test_prepare_waits_for_ready_and_reports_the_time_to_ready() -> None:
    api = Api(World(MemoryRepository()))
    w = api.world
    w.sync()
    started = w.clock.time()
    steps: list[str] = []

    def sleep(seconds: float) -> None:  # every wait moves the pollers on, like the real threads
        steps.append("sleep")
        if len(steps) == 3:
            w.acl.verify_all()
        w.sync()

    startup = StartupLog(w.repo, w.clock.time)
    startup.first_sync_done()
    sut = adapter(
        api, sleep=sleep, ready_started_at=started, ready_cap_s=300.0, startup_report=startup.report
    )
    sut.prepare(PERSONAS, ALL)
    events = sut.drain_events()
    ready = next(e for e in events if e["kind"] == "setup_b_ready")
    assert ready["ready"] is True and len(steps) >= 3
    assert ready["time_to_ready_s"] is not None and ready["time_to_ready_s"] > 0
    assert ready["waited_in_prepare_s"] <= ready["time_to_ready_s"] + 0.001
    assert ready["state"]["ready"] is True
    start = next(e for e in events if e["kind"] == "setup_b_startup")
    assert start["ready_at"] is not None and start["complete_at_first_sync"] is True
    # the recall sample comes AFTER readiness: nothing is measured before it
    kinds = [e["kind"] for e in events]
    assert kinds.index("setup_b_ready") < kinds.index("postfilter_recall")


def test_prepare_goes_on_after_the_cap_and_says_it_was_not_ready() -> None:
    api = Api(World(MemoryRepository()))
    api.world.sync()  # fresh, but never quiet and never verified
    slept: list[float] = []
    sut = adapter(
        api, sleep=lambda s: (slept.append(s), api.world.clock.sleep(s)), ready_cap_s=20.0
    )
    sut.prepare(PERSONAS, ALL)  # does not raise
    ready = next(e for e in sut.drain_events() if e["kind"] == "setup_b_ready")
    assert ready["ready"] is False and ready["time_to_ready_s"] is None
    assert sum(slept) >= 20.0 and ready["cap_s"] == 20.0


def test_reads_before_prepare_has_finished_do_not_exist_in_the_engine_order() -> None:
    """The engine prepares every system before the baseline reads begin (engine.py), so the wait
    in prepare() is a wait before any read that could count as a lockout."""
    import inspect

    from leaklab.runner import engine

    source = inspect.getsource(engine)
    assert source.index("system.prepare(personas, canaries)") < source.index("_read_cycle(")


# -- the start-up log -----------------------------------------------------------------------------------


def test_the_startup_log_compares_the_first_sync_with_the_state_later() -> None:
    w = World(MemoryRepository(), report_shared_changes=False)  # the delta will miss grant changes
    w.sync()
    log = StartupLog(w.repo, w.clock.time)
    first = log.first_sync_done()
    assert first == snapshot(w.repo) and first["grants"] > 0 and first["membership_edges"] > 0
    complete = log.report(ready_at=None)
    assert complete["complete_at_first_sync"] is True and complete["difference"] == {}
    assert complete["found_by_verify"] == 0 and complete["events_after_first_sync"] == {}
    w.apply(
        "grant_added",
        target="S1-Direct",
        principal_kind="user",
        principal_ref="p-intern",
        role="read",
    )
    w.sync()  # the fake delta does not report it
    w.acl.verify_all()  # the safety net finds it
    later = log.report(ready_at=123.0)
    assert later["complete_at_first_sync"] is False
    assert later["difference"]["grants"] >= 1 and later["ready_at"] == 123.0
    assert later["found_by_verify"] >= 1
    assert any(k.endswith(":verify") for k in later["events_after_first_sync"])  # type: ignore[attr-defined]
    assert P.startswith("CANARY-")  # (import used by the shared world helpers)
