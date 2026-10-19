"""The pure parts of the Setup B state: the nested-group closure and the reading of SharePoint claims."""

from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st

from leaklab.setup_b.state.closure import GROUP_PREFIX, USER_PREFIX, parse_login, transitive_groups
from leaklab.setup_b.state.model import (
    ROOT_SCOPE,
    AclSnapshot,
    DocRecord,
    Edge,
    Grant,
)


def edge(group: str, kind: Any, member: str) -> Edge:
    return Edge(group, kind, member)


def test_a_user_is_in_every_group_that_contains_a_group_that_contains_the_user() -> None:
    edges = {
        edge("all", "group", "eng"),
        edge("eng", "group", "plat"),
        edge("plat", "user", "paul"),
        edge("eng", "user", "eli"),
        edge("fin", "user", "fiona"),
    }
    closure = transitive_groups(edges)
    assert closure["paul"] == {"plat", "eng", "all"}
    assert closure["eli"] == {"eng", "all"}
    assert closure["fiona"] == {"fin"}
    assert set(closure) == {"paul", "eli", "fiona"}  # only users appear as keys


def test_a_nesting_cycle_neither_loops_nor_invents_groups() -> None:
    edges = {edge("a", "group", "b"), edge("b", "group", "a"), edge("b", "user", "u")}
    assert transitive_groups(edges) == {"u": {"a", "b"}}


def test_removing_the_only_edge_removes_the_user_from_the_closure() -> None:
    assert transitive_groups(set()) == {}


@settings(max_examples=60, deadline=None)
@given(
    nested=st.lists(st.tuples(st.integers(0, 5), st.integers(0, 5)), max_size=12),
    users=st.lists(st.tuples(st.integers(0, 5), st.integers(0, 3)), max_size=12),
)
def test_closure_matches_a_brute_force_reference(
    nested: list[tuple[int, int]], users: list[tuple[int, int]]
) -> None:
    edges = {edge(f"g{a}", "group", f"g{b}") for a, b in nested if a != b}
    edges |= {edge(f"g{g}", "user", f"u{u}") for g, u in users}
    got = transitive_groups(edges)
    # reference: top-down. g contains u if u is in g, or in any group inside g (fixpoint).
    members: dict[str, set[str]] = {f"g{i}": set() for i in range(6)}
    for e in edges:
        if e.member_kind == "user":
            members[e.group_id].add(e.member_id)
    changed = True
    while changed:
        changed = False
        for e in edges:
            if e.member_kind == "group":
                before = len(members[e.group_id])
                members[e.group_id] |= members[e.member_id]
                changed |= len(members[e.group_id]) != before
    expected: dict[str, set[str]] = {}
    for group, users_in in members.items():
        for user in users_in:
            expected.setdefault(user, set()).add(group)
    assert got == expected


def test_logins_are_read_as_the_verified_claim_formats() -> None:
    assert parse_login(f"{USER_PREFIX}Ll-P-Intern@T.onmicrosoft.com", 1) == (
        "user",
        "ll-p-intern@t.onmicrosoft.com",
    )
    gid = "00000000-0000-4000-8000-0000000000b2"
    assert parse_login(f"{GROUP_PREFIX}{gid}", 4) == ("entra_group", gid.lower())


def test_anything_else_is_unknown_so_it_can_never_grant() -> None:
    assert parse_login("c:0(.s|true", 4)[0] == "unknown"  # "everyone"-style special claim
    assert parse_login("c:0-.f|rolemanager|spo-grid-all-users/abc", 4)[0] == "unknown"
    assert parse_login(f"{USER_PREFIX}x@y", 4)[0] == "unknown"  # right prefix, wrong type
    assert parse_login(f"{GROUP_PREFIX}abc", 1)[0] == "unknown"
    assert parse_login("", 0) == ("unknown", "")


def test_only_read_roles_on_known_principals_grant_read() -> None:
    assert Grant("user", "a@b", "Read").grants_read
    assert Grant("entra_group", "g", "Edit").grants_read
    assert Grant("user", "a@b", "Full Control").grants_read
    assert not Grant("user", "a@b", "Limited Access").grants_read
    assert not Grant("user", "a@b", "Contribute").grants_read  # not verified: fail closed
    assert not Grant("unknown", "c:0(.s|true", "Read").grants_read


def test_an_items_scope_is_its_nearest_unique_ancestor_else_the_root() -> None:
    def doc(i: str, parent: str | None, unique: bool = False) -> DocRecord:
        return DocRecord(i, i, parent, True, None, unique)

    snap = AclSnapshot(
        items={
            "lib": doc("lib", None),
            "top": doc("top", "lib"),
            "uniq": doc("uniq", "top", True),
            "child": doc("child", "uniq"),
            "file": doc("file", "child"),
            "own": doc("own", "child", True),
        }
    )
    assert snap.scope_of("file") == "uniq"
    assert snap.scope_of("own") == "own"
    assert snap.scope_of("top") == ROOT_SCOPE
    assert snap.scope_of("unknown-item") == ROOT_SCOPE
