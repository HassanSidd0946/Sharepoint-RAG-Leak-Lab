"""Contract tests of the StateRepository: the same checks run on the in-memory and the Postgres one."""

from collections.abc import Callable

import pytest

from leaklab.setup_b.state.model import (
    ROOT_SCOPE,
    AclSnapshot,
    DocRecord,
    Edge,
    Event,
    Grant,
    PollerState,
)
from leaklab.setup_b.state.repository import StateRepository

Factory = Callable[[], StateRepository]


def snapshot() -> AclSnapshot:
    items = {
        "lib": DocRecord("lib", "root", None, True, "https://h/lib", False),
        "lab": DocRecord("lab", "ll-lab", "lib", True, "https://h/lib/ll-lab", False),
        "fold": DocRecord("fold", "Board", "lab", True, "https://h/lib/ll-lab/Board", True),
        "doc": DocRecord(
            "doc", "b-01.txt", "fold", False, "https://h/lib/ll-lab/Board/b-01.txt", False
        ),
        "gen": DocRecord("gen", "g-01.txt", "lab", False, "https://h/lib/ll-lab/g-01.txt", False),
    }
    grants = {
        ROOT_SCOPE: (
            Grant("entra_group", "g-all", "Read"),
            Grant("unknown", "c:0(.s|true", "Read"),
        ),
        "fold": (Grant("user", "ceo@t", "Read"), Grant("user", "hr@t", "Read", "sp_group:7")),
    }
    return AclSnapshot(items, grants)


def test_poller_state_round_trips_and_is_replaced(make_repo: Factory) -> None:
    repo = make_repo()
    assert repo.get_state("membership") is None
    repo.set_state(PollerState("membership", "link-1", 10.0, 11.0, None))
    assert repo.get_state("membership") == PollerState("membership", "link-1", 10.0, 11.0, None)
    repo.set_state(PollerState("membership", "link-2", 10.0, 20.0, "boom"))
    assert repo.get_state("membership") == PollerState("membership", "link-2", 10.0, 20.0, "boom")
    assert repo.get_state("acl") is None


def test_edges_and_the_closure_are_stored_together(make_repo: Factory) -> None:
    repo = make_repo()
    edges = {Edge("eng", "user", "paul"), Edge("all", "group", "eng")}
    repo.save_edges(edges, {"paul": {"eng", "all"}})
    assert repo.load_edges() == edges
    assert repo.groups_of("paul") == {"eng", "all"}
    assert repo.groups_of("nobody") == set()
    repo.save_edges({Edge("eng", "user", "paul")}, {"paul": {"eng"}})  # a save REPLACES
    assert repo.groups_of("paul") == {"eng"} and len(repo.load_edges()) == 1


def test_the_directory_maps_ids_to_upns(make_repo: Factory) -> None:
    repo = make_repo()
    repo.save_directory({"u1": "a@t", "u2": "b@t"})
    assert repo.load_directory() == {"u1": "a@t", "u2": "b@t"}
    assert repo.upn_of("u1") == "a@t" and repo.upn_of("missing") is None
    repo.save_directory({"u2": "b@t"})
    assert repo.upn_of("u1") is None


def test_the_acl_snapshot_round_trips_with_scopes_resolved(make_repo: Factory) -> None:
    repo = make_repo()
    snap = snapshot()
    repo.save_acl(snap)
    loaded = repo.load_acl()
    assert loaded.items == snap.items and loaded.grants == snap.grants
    assert repo.scope_of_item("doc") == "fold"  # inherits from the unique Board folder
    assert repo.scope_of_item("gen") == ROOT_SCOPE  # inherits all the way to the site root
    assert repo.scope_of_item("fold") == "fold"
    assert repo.scope_of_item("nope") is None
    assert {g.ref for g in repo.grants_of_scope("fold")} == {"ceo@t", "hr@t"}
    assert repo.grants_of_scope("doc") == ()  # only a scope has grants of its own


def test_a_second_save_replaces_the_first(make_repo: Factory) -> None:
    repo = make_repo()
    repo.save_acl(snapshot())
    repo.save_acl(AclSnapshot({"x": DocRecord("x", "x", None, False, None, False)}, {}))
    assert set(repo.load_acl().items) == {"x"} and repo.scope_of_item("doc") is None
    assert repo.grants_of_scope("fold") == ()


def test_events_keep_their_order_times_and_details(make_repo: Factory) -> None:
    repo = make_repo()
    repo.append_events([])
    repo.append_events(
        [
            Event("membership", "member_added", "g:user:u", {"group": "g"}, 1.5, 1.75),
            Event("acl", "grant_removed", "fold", {"role": "Read", "n": 2}, 2.0, 2.25),
            Event("membership", "member_removed", "g:user:u", {}, 3.0, 3.5),
        ]
    )
    assert [e.kind for e in repo.events()] == ["member_added", "grant_removed", "member_removed"]
    assert [e.kind for e in repo.events("membership")] == ["member_added", "member_removed"]
    first = repo.events()[0]
    assert (first.seen_at, first.applied_at, first.detail) == (1.5, 1.75, {"group": "g"})
    assert repo.events()[1].detail == {"role": "Read", "n": 2}


def test_atomic_commits_everything_or_nothing(make_repo: Factory) -> None:
    repo = make_repo()
    with pytest.raises(RuntimeError, match="stop"), repo.atomic():
        repo.save_edges({Edge("g", "user", "u")}, {"u": {"g"}})
        repo.append_events([Event("membership", "member_added", "g:user:u", {}, 1.0, 1.0)])
        repo.set_state(PollerState("membership", "l", 1.0, 1.0, None))
        raise RuntimeError("stop")
    assert repo.load_edges() == set() and repo.groups_of("u") == set()
    assert repo.events() == [] and repo.get_state("membership") is None
    with repo.atomic():
        repo.save_edges({Edge("g", "user", "u")}, {"u": {"g"}})
        repo.append_events([Event("membership", "member_added", "g:user:u", {}, 1.0, 1.0)])
    assert repo.groups_of("u") == {"g"} and len(repo.events()) == 1


def test_a_nested_atomic_block_rolls_back_with_the_outer_one(make_repo: Factory) -> None:
    repo = make_repo()
    with pytest.raises(RuntimeError), repo.atomic():
        repo.save_directory({"u": "a@t"})
        with repo.atomic():
            repo.save_acl(snapshot())
        raise RuntimeError("outer fails")
    assert repo.load_directory() == {} and repo.load_acl().items == {}


def test_an_unreachable_postgres_is_explained_without_leaking_the_password() -> None:
    from leaklab.setup_b.state.pg import PgRepository

    with pytest.raises(RuntimeError) as err:
        PgRepository("postgresql://leaklab:Sup3rSecret@127.0.0.1:1/leaklab")  # nothing listens on 1
    message = str(err.value)
    assert "port=1" in message and "dbname=leaklab" in message and "user=leaklab" in message
    assert "Sup3rSecret" not in message and "5433" in message  # the hint names the compose port
    with pytest.raises(RuntimeError, match="not a valid"):
        PgRepository("this is not a dsn=")
