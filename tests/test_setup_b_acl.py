"""acl_store and the access reader against the fake tenant (drive delta + REST in, decisions out)."""

from collections.abc import Callable

from leaklab.setup_b.state.acl import merge_verified
from leaklab.setup_b.state.model import ROOT_SCOPE, AclSnapshot, DocRecord, Grant
from leaklab.setup_b.state.repository import StateRepository
from leaklab.spec import generate_canaries
from runner_fakes import SPEC
from setup_b_fakes import FakeSharePointSource
from setup_b_world import World

Factory = Callable[[], StateRepository]
CAN = generate_canaries(SPEC)


def scope_grants(w: World, folder: str) -> set[Grant]:
    snap = w.repo.load_acl()
    item = next(i for i in snap.items.values() if i.name == folder and i.is_folder)
    return set(snap.grants.get(item.doc_id, ()))


def allowed(w: World, persona: str, canary: str) -> bool:
    return w.reader.can_read(w.uid(persona), w.doc_id(canary))


def test_the_first_poll_builds_the_tree_the_scopes_and_the_grants(make_repo: Factory) -> None:
    w = World(make_repo())
    m, a = w.sync()
    assert m.ok and a.ok
    snap = w.repo.load_acl()
    files = [i for i in snap.items.values() if not i.is_folder]
    unique = {i.name for i in snap.items.values() if i.unique}
    assert len(files) == 45
    assert unique == {
        "Engineering",
        "Platform-Secrets",
        "Finance",
        "Leadership",
        "HR",
        "Board",
        "S1-Entra",
        "S1-SPGroup",
        "S1-Direct",
        "S2-Nested",
        "offer-letter-draft.txt",
    }
    assert ROOT_SCOPE in snap.grants
    board = scope_grants(w, "Board")
    assert {g.kind for g in board} == {"entra_group", "user"} and {g.role for g in board} == {
        "Read"
    }
    assert {g.ref for g in board if g.kind == "user"} == {"ll-p-ceo@leaklabdev.onmicrosoft.com"}


def test_a_document_belongs_to_its_nearest_unique_scope(make_repo: Factory) -> None:
    w = World(make_repo())
    w.sync()
    snap = w.repo.load_acl()
    board_doc = w.doc_id(CAN["Board"][0])
    board_folder = next(i.doc_id for i in snap.items.values() if i.name == "Board")
    assert w.repo.scope_of_item(board_doc) == board_folder
    assert w.repo.scope_of_item(w.doc_id(CAN["General"][0])) == ROOT_SCOPE  # inherits the site root
    special = w.doc_id(CAN["special:f-direct-share"][0])
    assert w.repo.scope_of_item(special) == special  # a file with its own permissions


def test_decisions_follow_grants_and_nested_membership(make_repo: Factory) -> None:
    w = World(make_repo())
    w.sync()
    assert allowed(w, "p-plateng", CAN["Engineering"][0])  # via grp-engineering, two levels up
    assert allowed(w, "p-plateng", CAN["General"][0])  # via the root grant for grp-all-staff
    assert not allowed(w, "p-intern", CAN["Engineering"][0])
    assert allowed(w, "p-ceo", CAN["Board"][0])  # direct user grant + group
    assert not allowed(w, "p-ceo", CAN["General"][0])  # Board folder grants nothing on General
    assert allowed(w, "p-hr", CAN["special:f-direct-share"][0])
    assert not allowed(w, "p-intern", CAN["special:f-direct-share"][0])
    assert allowed(w, "p-plateng", CAN["S1-SPGroup"][0])  # SharePoint group expanded to its users
    assert allowed(w, "p-plateng", CAN["S1-Direct"][0])


def test_a_removed_direct_grant_is_seen_through_the_delta_and_logged(make_repo: Factory) -> None:
    w = World(make_repo())
    w.sync()
    assert allowed(w, "p-plateng", CAN["S1-Direct"][0])
    w.apply(
        "grant_removed",
        target="S1-Direct",
        principal_kind="user",
        principal_ref="p-plateng",
    )
    before = len(w.repo.events("acl"))
    result = w.sync()[1]
    assert result.ok and result.events == 1
    assert not allowed(w, "p-plateng", CAN["S1-Direct"][0])
    new = w.repo.events("acl")[before:]
    assert [e.kind for e in new] == ["grant_removed"]
    assert new[0].detail["found_by"] == "delta" and new[0].detail["scope_name"] == "S1-Direct"
    assert new[0].seen_at < new[0].applied_at
    assert w.acl.verify_all().events == 0  # the safety net finds nothing the delta missed


def test_a_sharepoint_group_membership_change_is_seen_and_the_user_loses_access(
    make_repo: Factory,
) -> None:
    w = World(make_repo())
    w.sync()
    w.apply(
        "group_member_removed", group="sp-s1-spgroup", member_kind="user", member_ref="p-plateng"
    )
    w.sync()
    assert not allowed(w, "p-plateng", CAN["S1-SPGroup"][0])
    kinds = [e.kind for e in w.repo.events("acl")]
    assert kinds[-1] == "grant_removed"
    removed = w.repo.events("acl")[-1].detail
    assert removed["via"].startswith("sp_group:") and removed["kind"] == "user"
    w.apply("group_member_added", group="sp-s1-spgroup", member_kind="user", member_ref="p-plateng")
    w.sync()
    assert allowed(w, "p-plateng", CAN["S1-SPGroup"][0])


def test_an_entra_membership_change_reaches_decisions_through_the_membership_store_only(
    make_repo: Factory,
) -> None:
    w = World(make_repo())
    w.sync()
    acl_events = len(w.repo.events("acl"))
    w.apply(
        "group_member_removed", group="grp-s1-entra", member_kind="user", member_ref="p-plateng"
    )
    m, a = w.sync()
    assert a.events == 0 and len(w.repo.events("acl")) == acl_events  # the drive delta is silent
    assert m.changes == 1
    assert not allowed(w, "p-plateng", CAN["S1-Entra"][0])


def test_moving_a_file_logs_it_and_a_unique_file_keeps_its_own_grants(make_repo: Factory) -> None:
    w = World(make_repo())
    w.sync()
    w.apply("file_moved", file="f-direct-share", to_folder="Board")
    result = w.sync()[1]
    assert result.ok
    assert [e.kind for e in w.repo.events("acl")][-1] == "item_moved"
    special = CAN["special:f-direct-share"][0]
    assert allowed(w, "p-hr", special) and not allowed(
        w, "p-finana", special
    )  # Board grants unused


def test_breaking_inheritance_makes_a_new_empty_scope(make_repo: Factory) -> None:
    w = World(make_repo())
    w.sync()
    assert allowed(w, "p-intern", CAN["General"][0])
    w.apply("inheritance_broken", target="General")
    w.sync()
    kinds = [e.kind for e in w.repo.events("acl")]
    assert "scope_became_unique" in kinds
    assert not allowed(w, "p-intern", CAN["General"][0])  # no grants copied: nobody can read
    assert not allowed(w, "p-plateng", CAN["General"][0])


def test_claims_it_cannot_read_never_grant_access(make_repo: Factory) -> None:
    w = World(make_repo())
    w.sync()
    root = w.repo.grants_of_scope(ROOT_SCOPE)
    unknown = [g for g in root if g.kind == "unknown"]
    assert unknown  # the site's own groups appear as claims it will not interpret
    assert all(not g.grants_read for g in unknown)
    # an Entra group placed INSIDE a SharePoint group is not expanded (unverified): it stays unknown
    group = next(iter(w.tenant.sp_groups.values()))
    entra = w.env.group_id("grp-hr")
    group["members"].add(f"c:0t.c|tenant|{entra}")
    w.sync()
    spg = scope_grants(w, "S1-SPGroup")
    inside = [g for g in spg if g.ref == entra.lower()]  # kept for audit, kind unknown
    assert inside and inside[0].kind == "unknown"
    assert not allowed(w, "p-hr", CAN["S1-SPGroup"][0])  # a member of grp-hr gets nothing from it


def test_limited_access_is_not_a_grant(make_repo: Factory) -> None:
    w = World(make_repo())
    w.sync()
    for scope, grants in w.repo.load_acl().grants.items():
        assert all(g.role != "Limited Access" for g in grants), scope


def test_a_delta_that_misses_a_change_is_caught_and_logged_by_the_verification(
    make_repo: Factory,
) -> None:
    w = World(make_repo(), report_shared_changes=False)  # a delta without the three headers
    w.sync()
    w.apply("grant_removed", target="S1-Direct", principal_kind="user", principal_ref="p-plateng")
    w.sync()
    assert allowed(w, "p-plateng", CAN["S1-Direct"][0])  # the missed change: still allowed (stale)
    result = w.acl.verify_all()
    assert result.ok and result.events == 1
    assert not allowed(w, "p-plateng", CAN["S1-Direct"][0])
    drift = w.repo.events("acl")[-1]
    assert drift.kind == "grant_removed" and drift.detail["found_by"] == "verify"


def test_a_change_at_the_site_root_is_only_found_by_the_verification(make_repo: Factory) -> None:
    w = World(make_repo())
    w.sync()
    assert not allowed(w, "p-ceo", CAN["General"][0])
    w.apply("grant_added", target="root", principal_kind="user", principal_ref="p-ceo", role="read")
    w.sync()
    assert not allowed(w, "p-ceo", CAN["General"][0])  # the drive delta does not report the root
    assert w.acl.verify_all().events == 1
    assert allowed(w, "p-ceo", CAN["General"][0])


def test_the_polling_fallback_rereads_unique_scopes_but_not_the_tree(make_repo: Factory) -> None:
    w = World(make_repo())
    w.sync()
    w.apply("grant_removed", target="S1-Direct", principal_kind="user", principal_ref="p-plateng")
    w.apply("inheritance_broken", target="General")
    w.sp.calls.clear()
    result = w.acl.poll_scopes()
    assert result.ok and result.events == 1  # the grant change, not the new unique scope
    assert not any(c.startswith("has_unique") for c in w.sp.calls)
    assert allowed(w, "p-intern", CAN["General"][0])  # the broken inheritance is not seen yet
    w.acl.verify_all()
    assert not allowed(w, "p-intern", CAN["General"][0])


def test_a_quiet_poll_makes_no_rest_calls(make_repo: Factory) -> None:
    w = World(make_repo())
    w.sync()
    w.sp.calls.clear()
    w.sync()
    assert w.sp.calls == []  # only the (cheap) delta call: no per-scope polling


def test_items_outside_the_container_are_not_tracked(make_repo: Factory) -> None:
    w = World(make_repo())
    other = w.tenant.graph.create_folder("drive1", None, "Other")
    w.tenant.graph.upload_text("drive1", other["id"], "secret.txt", "x")
    w.sync()
    names = {i.name for i in w.repo.load_acl().items.values()}
    assert "Other" not in names and "secret.txt" not in names and "ll-lab" in names


def test_a_failed_poll_keeps_the_old_state_and_the_last_success(make_repo: Factory) -> None:
    w = World(make_repo())
    w.sync()
    good = w.repo.get_state("acl")
    snap = w.repo.load_acl()
    w.apply("grant_removed", target="S1-Direct", principal_kind="user", principal_ref="p-plateng")
    w.drive.fail_next = TimeoutError("sharepoint timed out")
    result = w.sync()[1]
    state = w.repo.get_state("acl")
    assert not result.ok and state is not None and good is not None
    assert state.last_success_at == good.last_success_at and "timed out" in (state.last_error or "")
    assert w.repo.load_acl().grants == snap.grants
    assert w.sync()[1].ok and not allowed(w, "p-plateng", CAN["S1-Direct"][0])  # caught up


# -- fail closed: stale data is never trusted --------------------------------------------------------


def test_stale_state_denies_with_a_reason(make_repo: Factory) -> None:
    w = World(make_repo(), max_age_s=30.0)
    uid, doc = w.uid("p-plateng"), w.doc_id(CAN["General"][0])
    assert w.reader.decide(uid, doc).reason == "stale_acl"  # nothing polled yet
    w.sync()
    assert w.reader.decide(uid, doc).allowed and w.reader.freshness().fresh
    w.clock.sleep(31.0)
    assert w.reader.decide(uid, doc).reason == "stale_acl"  # both stale: nothing is trusted
    assert w.reader.readable(uid) == set()
    w.membership.poll_once()  # membership fresh again, the ACL store still stale
    assert w.reader.decide(uid, doc).reason == "stale_acl"
    w.acl.poll_once()
    assert w.reader.decide(uid, doc).allowed


def test_a_stale_membership_state_denies_group_grants_but_keeps_direct_ones(
    make_repo: Factory,
) -> None:
    w = World(make_repo(), max_age_s=12.0)
    w.sync()
    via_group, direct = w.doc_id(CAN["General"][0]), w.doc_id(CAN["S1-Direct"][0])
    uid = w.uid("p-plateng")
    assert w.reader.decide(uid, via_group).via is not None
    assert w.reader.decide(uid, direct).via == f"user:{w.repo.upn_of(uid)}"
    for _ in range(3):  # membership polls fail, ACL polls keep succeeding
        w.group_src.fail_next = ConnectionError("down")
        w.clock.sleep(5.0)
        w.membership.poll_once()
        w.acl.poll_once()
    fresh = w.reader.freshness()
    assert fresh.acl_fresh and not fresh.membership_fresh
    assert w.reader.decide(uid, via_group).reason == "stale_membership"
    assert w.reader.decide(uid, direct).allowed  # a grant that names the user needs no groups
    readable = w.reader.readable(uid)
    assert direct in readable and via_group not in readable
    assert all((w.reader.decide(uid, d).via or "").startswith("user:") for d in readable)


def test_a_run_of_failed_polls_makes_the_state_go_stale_and_deny(make_repo: Factory) -> None:
    w = World(make_repo(), max_age_s=12.0)
    w.sync()
    uid, doc = w.uid("p-plateng"), w.doc_id(CAN["General"][0])
    assert w.reader.can_read(uid, doc)
    for _ in range(3):
        w.group_src.fail_next = ConnectionError("down")
        w.sync()
    assert (
        not w.reader.can_read(uid, doc) and w.reader.decide(uid, doc).reason == "stale_membership"
    )


def test_unknown_documents_and_users_are_denied(make_repo: Factory) -> None:
    w = World(make_repo())
    w.sync()
    assert w.reader.decide(w.uid("p-plateng"), "no-such-doc").reason == "no_such_document"
    stranger = w.reader.decide("not-a-user", w.doc_id(CAN["Board"][0]))
    assert not stranger.allowed and stranger.reason == "unknown_principal"


def test_readable_lists_exactly_the_files_a_user_may_read(make_repo: Factory) -> None:
    w = World(make_repo())
    w.sync()
    readable = w.reader.readable(w.uid("p-intern"))
    assert readable == {w.doc_id(c) for c in CAN["General"]}
    assert w.reader.readable("not-a-user") == set()


def test_the_event_log_orders_changes_and_times_are_consistent(make_repo: Factory) -> None:
    w = World(make_repo())
    w.sync()
    w.apply("grant_removed", target="S1-Direct", principal_kind="user", principal_ref="p-plateng")
    w.apply(
        "group_member_removed", group="grp-s1-entra", member_kind="user", member_ref="p-plateng"
    )
    w.sync()
    events = w.repo.events()
    assert events and all(e.seen_at <= e.applied_at for e in events)
    tail = [(e.source, e.kind) for e in events[-2:]]
    assert ("membership", "member_removed") in tail and ("acl", "grant_removed") in tail


def test_a_user_in_no_group_still_matches_a_direct_grant(make_repo: Factory) -> None:
    """Regression found by the oracle property test: p-hr's only group was grp-hr; once p-hr left it
    the directory forgot p-hr's UPN and the direct grant on the special file stopped matching."""
    w = World(make_repo())
    w.sync()
    special = CAN["special:f-direct-share"][0]
    assert allowed(w, "p-hr", special)
    w.apply("group_member_removed", group="grp-hr", member_kind="user", member_ref="p-hr")
    w.sync()
    assert w.repo.groups_of(w.uid("p-hr")) == set()  # in no group at all now
    assert allowed(w, "p-hr", special)  # the direct grant still works
    assert not allowed(w, "p-hr", CAN["HR"][0])  # and the group grant is gone
    assert w.repo.upn_of(w.uid("p-hr")) == "ll-p-hr@leaklabdev.onmicrosoft.com"


def test_a_directory_that_cannot_resolve_means_a_user_grant_denies(make_repo: Factory) -> None:
    w = World(make_repo())
    w.directory.fail = True
    w.sync()
    assert w.repo.upn_of(w.uid("p-ceo")) is None
    assert not allowed(w, "p-ceo", CAN["special:f-direct-share"][0])  # unresolved: fail closed
    assert allowed(w, "p-ceo", CAN["Board"][0])  # the group grant needs no UPN
    w.directory.fail = False
    w.sync()
    assert allowed(w, "p-ceo", CAN["special:f-direct-share"][0])


class RacySharePoint(FakeSharePointSource):
    """Returns the OLD answer for one scope, and only then lets a change happen and be polled: the
    verification's read is then older than the store's state."""

    def __init__(self, tenant: object, after_read: Callable[[], None]) -> None:
        super().__init__(tenant)  # type: ignore[arg-type]
        self.after_read: Callable[[], None] | None = after_read

    def assignments(self, item: DocRecord | None):  # type: ignore[no-untyped-def]
        result = super().assignments(item)
        if item is not None and item.name == "S1-Direct" and self.after_read is not None:
            hook, self.after_read = self.after_read, None
            hook()
        return result


def test_a_poll_that_lands_during_the_verification_is_not_overwritten_by_it(
    make_repo: Factory,
) -> None:
    w = World(make_repo())
    w.sync()

    def change_and_poll() -> None:  # runs between the verification's read and its merge
        w.apply(
            "grant_removed", target="S1-Direct", principal_kind="user", principal_ref="p-plateng"
        )
        assert w.acl.poll_once().ok  # the lock is free: the verification holds none while reading

    w.acl._sp = RacySharePoint(w.tenant, change_and_poll)  # type: ignore[assignment]
    assert allowed(w, "p-plateng", CAN["S1-Direct"][0])
    result = w.acl.verify_all()
    assert result.ok
    assert not allowed(w, "p-plateng", CAN["S1-Direct"][0])  # the NEWER state survived the merge
    verify_events = [e for e in w.repo.events("acl") if e.detail.get("found_by") == "verify"]
    assert not [
        e for e in verify_events if e.kind == "grant_added"
    ]  # it did not resurrect the grant
    assert w.acl.verify_all().events == 0  # and a later verification finds the store consistent


def test_merge_verified_keeps_what_moved_on_and_takes_the_rest() -> None:
    def item(i: str, unique: bool = False) -> DocRecord:
        return DocRecord(i, i, "p", True, None, unique)

    g_old, g_new, g_fresh = (Grant("user", u, "Read") for u in ("old@t", "new@t", "fresh@t"))
    start = AclSnapshot(
        {"a": item("a", True), "b": item("b", True)}, {"a": (g_old,), "b": (g_old,)}
    )
    current = AclSnapshot(
        {"a": item("a", True), "b": item("b", True)}, {"a": (g_new,), "b": (g_old,)}
    )  # a poll changed scope a while the verification was reading
    work = AclSnapshot(
        {"a": item("a", True), "b": item("b", True)}, {"a": (g_old,), "b": (g_fresh,)}
    )
    merged = merge_verified(current, start, work)
    assert merged.grants["a"] == (g_new,)  # a: the poll's newer state wins
    assert merged.grants["b"] == (
        g_fresh,
    )  # b: untouched meanwhile, the verification's read applies
    assert current.grants["a"] == (g_new,)  # the inputs are not modified


def test_merge_verified_drops_the_grants_of_a_scope_that_stopped_being_one() -> None:
    def item(i: str, unique: bool) -> DocRecord:
        return DocRecord(i, i, "p", True, None, unique)

    g = Grant("user", "a@t", "Read")
    start = AclSnapshot({"a": item("a", True)}, {"a": (g,)})
    work = AclSnapshot({"a": item("a", False)}, {"a": (g,)})  # inheritance came back
    merged = merge_verified(start, start, work)
    assert merged.items["a"].unique is False and "a" not in merged.grants
