"""membership_store against the fake tenant: group delta in, closure and event log out."""

from collections.abc import Callable

import pytest

from leaklab.setup_b.state.model import Edge, ResyncRequired
from leaklab.setup_b.state.repository import StateRepository
from setup_b_world import World

Factory = Callable[[], StateRepository]


def groups_of(world: World, persona: str) -> set[str]:
    """The persona's groups, as spec group ids."""
    ids = {world.env.group_id(g.id): g.id for g in world.env.plan.groups}
    return {ids[g] for g in world.repo.groups_of(world.uid(persona)) if g in ids}


def test_the_first_poll_loads_everything_and_computes_the_nested_closure(
    make_repo: Factory,
) -> None:
    w = World(make_repo())
    result = w.membership.poll_once()
    assert result.ok and result.full and result.changes == len(w.repo.load_edges())
    # p-plateng is in grp-eng-platform, inside grp-engineering, inside grp-all-staff
    assert groups_of(w, "p-plateng") == {
        "grp-eng-platform",
        "grp-engineering",
        "grp-all-staff",
        "grp-s1-entra",
    }
    assert groups_of(w, "p-intern") == {"grp-all-staff"}
    assert groups_of(w, "p-finlead") == {"grp-finance-leads", "grp-finance", "grp-all-staff"}
    assert w.repo.upn_of(w.uid("p-plateng")) == "ll-p-plateng@leaklabdev.onmicrosoft.com"


def test_removing_a_user_changes_the_closure_and_logs_one_event_with_both_times(
    make_repo: Factory,
) -> None:
    w = World(make_repo())
    w.sync()
    w.apply(
        "group_member_removed", group="grp-s1-entra", member_kind="user", member_ref="p-plateng"
    )
    before = len(w.repo.events("membership"))
    result = w.sync()[0]
    assert result.ok and result.changes == 1 and not result.full
    assert "grp-s1-entra" not in groups_of(w, "p-plateng")
    new = w.repo.events("membership")[before:]
    assert [e.kind for e in new] == ["member_removed"]
    assert new[0].detail["member"] == w.uid("p-plateng")
    assert new[0].seen_at < new[0].applied_at  # the poll returned, then the commit followed
    assert new[0].applied_at <= w.clock.time()


def test_removing_a_nested_group_removes_everything_the_user_got_through_it(
    make_repo: Factory,
) -> None:
    w = World(make_repo())
    w.sync()
    w.apply(
        "group_member_removed",
        group="grp-engineering",
        member_kind="entra_group",
        member_ref="grp-eng-platform",
    )
    w.sync()
    assert groups_of(w, "p-plateng") == {"grp-eng-platform", "grp-s1-entra"}  # S2
    assert groups_of(w, "p-intern") == {"grp-all-staff"}  # unaffected
    w.apply(
        "group_member_added",
        group="grp-engineering",
        member_kind="entra_group",
        member_ref="grp-eng-platform",
    )
    w.sync()
    assert "grp-all-staff" in groups_of(w, "p-plateng")


def test_a_poll_without_changes_logs_nothing_but_moves_last_success(make_repo: Factory) -> None:
    w = World(make_repo())
    w.membership.poll_once()
    first = w.repo.get_state("membership")
    assert first is not None and first.last_success_at is not None
    events = len(w.repo.events("membership"))
    result = w.sync()[0]
    second = w.repo.get_state("membership")
    assert result.ok and result.changes == 0 and len(w.repo.events("membership")) == events
    assert second is not None and second.last_success_at is not None
    assert second.last_success_at > first.last_success_at
    assert w.group_src.fetches == [None, "token"]  # the second poll continued from the saved link


def test_a_failed_poll_changes_nothing_records_why_and_keeps_the_last_success(
    make_repo: Factory,
) -> None:
    w = World(make_repo())
    w.membership.poll_once()
    good = w.repo.get_state("membership")
    edges = w.repo.load_edges()
    w.apply(
        "group_member_removed", group="grp-s1-entra", member_kind="user", member_ref="p-plateng"
    )
    w.group_src.fail_next = ConnectionError("graph down")
    result = w.sync()[0]
    state = w.repo.get_state("membership")
    assert not result.ok and "ConnectionError" in (result.error or "")
    assert w.repo.load_edges() == edges  # the old state stands
    assert state is not None and good is not None
    assert state.last_success_at == good.last_success_at and state.link == good.link
    assert state.last_error and "graph down" in state.last_error
    assert w.sync()[0].ok  # the next poll catches up on what the failed one missed
    assert "grp-s1-entra" not in groups_of(w, "p-plateng")
    recovered = w.repo.get_state("membership")
    assert recovered is not None and recovered.last_error is None


def test_a_dead_link_triggers_a_full_resync_that_logs_only_the_differences(
    make_repo: Factory,
) -> None:
    w = World(make_repo())
    w.sync()
    w.apply(
        "group_member_removed", group="grp-s1-entra", member_kind="user", member_ref="p-plateng"
    )
    w.group_src.resync_next = True
    events_before = len(w.repo.events("membership"))
    result = w.sync()[0]
    assert result.ok and result.full
    assert w.group_src.fetches[-2:] == ["token", None]  # tried the link, then enumerated everything
    new = w.repo.events("membership")[events_before:]
    assert [e.kind for e in new] == ["member_removed"]  # not 18 "added" events again
    assert "grp-s1-entra" not in groups_of(w, "p-plateng")


def test_a_deleted_group_loses_its_edges_and_its_place_inside_other_groups(
    make_repo: Factory,
) -> None:
    w = World(make_repo())
    w.sync()
    gid = w.env.group_id("grp-eng-platform")
    victim = next(n for n, g in w.tenant.groups.items() if g["id"] == gid)
    del w.tenant.groups[victim]
    w.sync()
    assert not [e for e in w.repo.load_edges() if gid in (e.group_id, e.member_id)]
    assert groups_of(w, "p-plateng") == {"grp-s1-entra"}


def test_an_unresolvable_directory_means_no_upn_not_an_error(make_repo: Factory) -> None:
    w = World(make_repo())
    w.directory.fail = True
    result = w.membership.poll_once()
    assert result.ok  # membership still works: group grants do not need a UPN
    assert w.repo.upn_of(w.uid("p-plateng")) is None
    assert groups_of(w, "p-plateng")
    w.directory.fail = False
    w.sync()
    assert w.repo.upn_of(w.uid("p-plateng")) is not None  # resolved at the next poll


def test_the_edges_are_the_direct_membership_only(make_repo: Factory) -> None:
    w = World(make_repo())
    w.sync()
    edges = w.repo.load_edges()
    eng, plat = w.env.group_id("grp-engineering"), w.env.group_id("grp-eng-platform")
    assert Edge(eng, "group", plat) in edges
    assert Edge(eng, "user", w.uid("p-plateng")) not in edges  # reached only through nesting


def test_resync_error_type_is_the_one_the_sources_raise() -> None:
    assert issubclass(ResyncRequired, Exception)
    with pytest.raises(ResyncRequired):
        raise ResyncRequired("410")
