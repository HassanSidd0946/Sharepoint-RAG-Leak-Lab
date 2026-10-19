"""The generator end to end against the in-memory fake tenant (tests/generator_fakes.py)."""

from pathlib import Path

import pytest

from generator_fakes import FakeClock, FakeTenant
from leaklab.generator import (
    Env,
    GeneratorError,
    GenSettings,
    MutationLog,
    apply_mutation,
    generate,
    teardown,
    verify,
)
from leaklab.generator.env import group_claim, user_login
from leaklab.oracle import Mutation, MutationType, expected_access
from leaklab.sp_rest import SPRestError
from leaklab.spec import generate_canaries, load_spec

SPEC_PATH = Path(__file__).resolve().parents[1] / "tenant_spec.yaml"
SPEC = load_spec(SPEC_PATH)
CAN = generate_canaries(SPEC)


def make_env(tenant: FakeTenant, clock: FakeClock | None = None, **settings: float) -> Env:
    clock = clock or FakeClock()
    return Env(
        spec=SPEC,
        graph=tenant.graph,
        sp=tenant.sp,
        settings=GenSettings(
            password="pw",
            usage_location="US",
            site_host="t.sharepoint.com",
            site_path="/sites/T",
            membership_settle_s=20.0,
            **settings,  # type: ignore[arg-type]
        ),
        log=MutationLog(run_id="test", created_at=0.0),
        sleep=clock.sleep,
        clock=clock.time,
    )


@pytest.fixture
def built() -> tuple[FakeTenant, Env]:
    tenant = FakeTenant()
    env = make_env(tenant)
    generate(env)
    return tenant, env


def errors(report: object) -> list[str]:
    return [i.what for i in report.issues if i.severity == "error"]  # type: ignore[attr-defined]


# -- generate and verify -----------------------------------------------------------------


def test_generate_builds_the_tenant_and_verify_agrees_with_the_spec(
    built: tuple[FakeTenant, Env],
) -> None:
    tenant, env = built
    report = verify(env)
    assert report.ok, report.issues
    assert report.checked["users"] == 12 and report.checked["groups"] == 10
    assert report.checked["sp_groups"] == 1
    assert report.checked["documents"] == 45  # 44 folder documents + the special file
    assert len(tenant.users) == 12 and len(tenant.groups) == 10
    # licensed users hold the license, unlicensed do not
    assert tenant.users[f"ll-p-intern@{SPEC.naming.domain}"]["licenses"] == {"sku-1"}
    assert not tenant.users[f"ll-p-hr@{SPEC.naming.domain}"]["licenses"]


def test_everything_lives_under_the_prefixed_container(built: tuple[FakeTenant, Env]) -> None:
    tenant, env = built
    top = [i for i in tenant.items.values() if i.parent is None]
    assert [i.name for i in top] == ["ll-lab"]
    names = {i.name for i in tenant.items.values()}
    assert {"General", "Engineering", "Platform-Secrets", "Leadership", "Board"} <= names


def test_permissions_match_the_spec_and_break_back_to_back(built: tuple[FakeTenant, Env]) -> None:
    tenant, env = built
    assert env.sp.has_unique(env.folder_item("Engineering/Platform-Secrets"))
    assert not env.sp.has_unique(env.folder_item("General"))
    grants = {
        a.login: {r.name for r in a.roles} for a in env.sp.assignments(env.folder_item("Board"))
    }
    assert grants == {
        group_claim(env.group_id("grp-board")): {"Read"},
        user_login(f"ll-p-ceo@{SPEC.naming.domain}"): {"Read"},
    }
    special = env.sp.assignments(env.file_item("General", "offer-letter-draft.txt"))
    assert {a.login for a in special} == {
        user_login(f"ll-p-hr@{SPEC.naming.domain}"),
        user_login(f"ll-p-ceo@{SPEC.naming.domain}"),
    }
    # the site-root grant for all-staff is present; the admin was not granted anything
    root = {a.login for a in env.sp.assignments("/web")}
    assert group_claim(env.group_id("grp-all-staff")) in root


def test_a_second_run_changes_nothing(built: tuple[FakeTenant, Env]) -> None:
    tenant, env = built
    before = tenant.snapshot()
    again = generate(make_env(tenant))
    assert tenant.snapshot() == before
    for created in (
        "users_created",
        "groups_created",
        "folders_created",
        "files_created",
        "inheritance_broken",
        "assignments_added",
        "root_assignments_added",
        "licenses_assigned",
        "members_added",
    ):
        assert again.counts.get(created, 0) == 0, (created, again.counts)
    assert again.counts["members_already"] > 0  # "already a member" counted as success
    assert verify(env).ok


def test_generate_retries_a_404_on_the_first_member_adds() -> None:
    tenant = FakeTenant()
    tenant.add_member_404s = 3
    clock = FakeClock()
    generate(make_env(tenant, clock))
    assert tenant.add_member_404s_raised == 3
    assert clock.slept.count(5.0) >= 3  # it waited between attempts


def test_generate_retries_ensureuser_not_found_up_to_two_minutes() -> None:
    tenant = FakeTenant()
    tenant.ensure_failures = 4
    clock = FakeClock()
    generate(make_env(tenant, clock))
    assert tenant.ensure_failures_raised == 4
    # a longer failure than the timeout is raised, not swallowed
    stubborn = FakeTenant()
    stubborn.ensure_failures = 10_000
    with pytest.raises(SPRestError):
        generate(make_env(stubborn, FakeClock()))


def test_inheritance_drift_stops_the_run_with_a_clear_error(built: tuple[FakeTenant, Env]) -> None:
    tenant, env = built
    general = next(i for i in tenant.items.values() if i.name == "General")
    general.unique = True
    with pytest.raises(GeneratorError, match="drift"):
        generate(make_env(tenant))


def test_rerun_removes_extra_grants_on_unique_items_but_leaves_limited_access(
    built: tuple[FakeTenant, Env],
) -> None:
    tenant, env = built
    board = next(i for i in tenant.items.values() if i.name == "Board")
    intern = env.principal("user", "p-intern")
    env.sp.add_assignment(env.folder_item("Board"), intern.sp_id, env.role_id("Read"))
    assert intern.login in board.grants
    report = generate(make_env(tenant))
    assert intern.login not in board.grants  # reconciled back to the spec
    assert report.counts["extra_assignments_removed"] == 1
    assert verify(env).ok  # Limited Access entries do not count as extras


def test_verify_reports_missing_documents_unexpected_grants_and_memberships(
    built: tuple[FakeTenant, Env],
) -> None:
    tenant, env = built
    doc = next(i for i in tenant.items.values() if i.name == "ll-hr-01.txt")
    del tenant.items[doc.id]
    board = next(i for i in tenant.items.values() if i.name == "Board")
    board.grants["i:0#.f|membership|stranger@x.com"] = {"Read"}
    plat = tenant.groups["ll-grp-eng-platform"]
    plat["members"] = {m for m in plat["members"] if m[0] != "user"}
    report = verify(env)
    assert not report.ok
    problems = " | ".join(errors(report))
    assert "ll-hr-01.txt" in problems
    assert "unexpected grant" in problems
    assert f"group membership of ll-p-plateng@{SPEC.naming.domain}" in problems


def test_verify_treats_a_nonempty_default_members_group_as_a_warning(
    built: tuple[FakeTenant, Env],
) -> None:
    tenant, env = built
    tenant.sp.default_members["member"] = ["someone"]
    report = verify(env)
    assert report.ok
    assert any(i.severity == "warning" and "members" in i.what for i in report.issues)


# -- teardown ----------------------------------------------------------------------------


def test_teardown_removes_only_prefixed_objects(built: tuple[FakeTenant, Env]) -> None:
    tenant, env = built
    foreign_folder = tenant.graph.create_folder(
        "drive1", None, "General"
    )  # same name as a spec folder
    tenant.graph.create_user("someone@contoso.onmicrosoft.com", "S", "someone", "pw", "US")
    tenant.graph.create_group("Team Site Members")
    owners_before = dict(tenant.web_grants)["LeakLab Owners"]

    report = teardown(make_env(tenant))
    assert report.counts["container_deleted"] == 1
    assert report.counts["groups_deleted"] == 10 and report.counts["users_deleted"] == 12
    assert report.counts["sp_groups_deleted"] == 1
    assert report.counts["root_assignments_removed"] == 1

    assert [i.name for i in tenant.items.values()] == ["General"]  # only the foreign folder remains
    assert tenant.items[foreign_folder["id"]].parent is None
    assert set(tenant.users) == {"someone@contoso.onmicrosoft.com"}
    assert set(tenant.groups) == {"Team Site Members"}
    assert tenant.web_grants["LeakLab Owners"] == owners_before  # the site's own groups untouched
    assert group_claim_in(tenant) == set()  # our root grant is gone


def group_claim_in(tenant: FakeTenant) -> set[str]:
    return {
        k for k, v in tenant.web_grants.items() if k.startswith("c:0t.c|tenant|") and "Read" in v
    }


def test_teardown_twice_is_a_noop_and_generate_works_again(built: tuple[FakeTenant, Env]) -> None:
    tenant, _ = built
    teardown(make_env(tenant))
    second = teardown(make_env(tenant))
    assert second.counts == {}
    generate(make_env(tenant))
    assert verify(make_env(tenant)).ok


# -- mutations and the log the oracle consumes -------------------------------------------


def grant_removed(target: str, kind: str, ref: str) -> Mutation:
    return Mutation(
        type=MutationType.GRANT_REMOVED,
        params={"target": target, "principal_kind": kind, "principal_ref": ref},
    )


def test_s1_revocation_is_logged_with_confirmed_at_and_the_oracle_follows(
    built: tuple[FakeTenant, Env],
) -> None:
    tenant, env = built
    entry = apply_mutation(env, grant_removed("Engineering", "entra_group", "grp-engineering"))
    assert entry.status == "confirmed"
    assert entry.requested_at <= entry.executed_at <= entry.confirmed_at  # type: ignore[operator]
    mutations = env.log.oracle_mutations()
    assert [m.confirmed_at for m in mutations] == [entry.confirmed_at]
    t = entry.confirmed_at
    assert t is not None
    before = expected_access(SPEC, mutations, t - 1)["p-plateng"]
    after = expected_access(SPEC, mutations, t)["p-plateng"]
    assert set(CAN["Engineering"]) <= before and not set(CAN["Engineering"]) & after
    # the fake tenant really has no grant for the group any more
    grants = env.sp.assignments(env.folder_item("Engineering"))
    assert all(a.login != group_claim(env.group_id("grp-engineering")) for a in grants)


def test_s2_nested_group_removal_is_confirmed_through_a_witness_user(
    built: tuple[FakeTenant, Env],
) -> None:
    _, env = built
    entry = apply_mutation(
        env,
        Mutation(
            type=MutationType.GROUP_MEMBER_REMOVED,
            params={
                "group": "grp-engineering",
                "member_kind": "entra_group",
                "member_ref": "grp-eng-platform",
            },
        ),
    )
    assert entry.status == "confirmed" and "witness" in entry.note
    after = expected_access(SPEC, env.log.oracle_mutations(), entry.confirmed_at or 0.0)
    arms = set(CAN["S1-Entra"]) | set(CAN["S1-SPGroup"]) | set(CAN["S1-Direct"])
    assert after["p-plateng"] == set(CAN["Engineering/Platform-Secrets"]) | arms
    # and adding it back is confirmed too
    back = apply_mutation(
        env,
        Mutation(
            type=MutationType.GROUP_MEMBER_ADDED,
            params={
                "group": "grp-engineering",
                "member_kind": "entra_group",
                "member_ref": "grp-eng-platform",
            },
        ),
    )
    assert back.status == "confirmed"


def test_s4_moving_the_special_file_keeps_its_grants(built: tuple[FakeTenant, Env]) -> None:
    tenant, env = built
    before = {
        a.login for a in env.sp.assignments(env.file_item("General", "offer-letter-draft.txt"))
    }
    entry = apply_mutation(
        env,
        Mutation(
            type=MutationType.FILE_MOVED, params={"file": "f-direct-share", "to_folder": "Board"}
        ),
    )
    assert entry.status == "confirmed"
    after = {a.login for a in env.sp.assignments(env.file_item("Board", "offer-letter-draft.txt"))}
    assert after == before
    assert env.special_folder["f-direct-share"] == "Board"


def test_a_change_that_never_shows_up_stays_unconfirmed_and_intent_does_not_change(
    built: tuple[FakeTenant, Env],
) -> None:
    tenant, env = built
    tenant.ignore_removals = True  # the removal call "succeeds" but nothing happens
    entry = apply_mutation(env, grant_removed("Engineering", "entra_group", "grp-engineering"))
    assert entry.status == "unconfirmed" and entry.confirmed_at is None
    [mutation] = env.log.oracle_mutations()
    assert mutation.confirmed_at is None
    assert expected_access(SPEC, [mutation], 1e12) == expected_access(SPEC, [], 0.0)


def test_unsupported_and_invalid_mutations_are_logged_as_failed_and_left_out(
    built: tuple[FakeTenant, Env],
) -> None:
    _, env = built
    with pytest.raises(GeneratorError, match="not supported"):
        apply_mutation(
            env, Mutation(type=MutationType.INHERITANCE_RESTORED, params={"target": "Board"})
        )
    with pytest.raises(GeneratorError, match="inherits"):
        apply_mutation(
            env,
            Mutation(
                type=MutationType.GRANT_ADDED,
                params={
                    "target": "General",
                    "principal_kind": "user",
                    "principal_ref": "p-hr",
                    "role": "read",
                },
            ),
        )
    assert [e.status for e in env.log.entries] == ["failed", "failed"]
    assert env.log.oracle_mutations() == []


def test_the_mutation_log_round_trips_through_a_file(
    built: tuple[FakeTenant, Env], tmp_path: Path
) -> None:
    _, env = built
    apply_mutation(env, grant_removed("Finance", "entra_group", "grp-finance"))
    path = tmp_path / "run" / "mutations.json"
    env.log.save(path)
    loaded = MutationLog.load(path)
    assert loaded == env.log
    assert loaded.oracle_mutations() == env.log.oracle_mutations()


def test_generate_refuses_to_run_without_a_password_or_usage_location() -> None:
    tenant = FakeTenant()
    env = make_env(tenant)
    env.settings.usage_location = ""
    with pytest.raises(GeneratorError, match="usage location"):
        generate(env)
    env2 = make_env(FakeTenant())
    env2.settings.password = ""
    with pytest.raises(GeneratorError, match="PASSWORD"):
        generate(env2)


# -- SharePoint groups and the S1 control arms ---------------------------------------------------


def sp_login(tenant: FakeTenant, user_id: str) -> str:
    return user_login(f"ll-{user_id}@{SPEC.naming.domain}")


def test_the_sharepoint_group_is_created_filled_and_granted_on_its_folder(
    built: tuple[FakeTenant, Env],
) -> None:
    tenant, env = built
    group = tenant.sp_groups["ll-sp-s1-spgroup"]
    assert group["members"] == {sp_login(tenant, "p-plateng")}
    grants = env.sp.assignments(env.folder_item("S1-SPGroup"))
    assert [(a.login, [r.name for r in a.roles if r.name != "Limited Access"]) for a in grants] == [
        ("ll-sp-s1-spgroup", ["Read"])
    ]
    direct = env.sp.assignments(env.folder_item("S1-Direct"))
    assert [a.login for a in direct if any(r.name == "Read" for r in a.roles)] == [
        sp_login(tenant, "p-plateng")
    ]


def test_verify_catches_a_wrong_sharepoint_group_member(built: tuple[FakeTenant, Env]) -> None:
    tenant, env = built
    tenant.sp_groups["ll-sp-s1-spgroup"]["members"].add(sp_login(tenant, "p-intern"))
    report = verify(env)
    assert not report.ok
    assert any("members of SharePoint group" in w for w in errors(report))


def test_a_second_generate_changes_nothing_including_sharepoint_groups(
    built: tuple[FakeTenant, Env],
) -> None:
    tenant, env = built
    before = tenant.snapshot()
    again = generate(make_env(tenant))
    assert tenant.snapshot() == before
    assert again.counts.get("sp_groups_created", 0) == 0
    assert again.counts["sp_groups_reused"] == 1 and again.counts["sp_members_already"] == 1


@pytest.mark.parametrize(
    ("scenario", "lost"),
    [("S1-entra", "S1-Entra"), ("S1-spgroup", "S1-SPGroup"), ("S1-direct", "S1-Direct")],
)
def test_each_s1_scenario_applies_confirms_and_feeds_the_oracle(
    built: tuple[FakeTenant, Env], scenario: str, lost: str
) -> None:
    _, env = built
    sc = next(s for s in SPEC.scenarios if s.id == scenario)
    [spec_mutation] = sc.mutations
    entry = apply_mutation(env, Mutation(type=spec_mutation.type, params=spec_mutation.params))  # type: ignore[arg-type]
    assert entry.status == "confirmed" and entry.confirmed_at is not None
    exp = expected_access(SPEC, env.log.oracle_mutations(), entry.confirmed_at)
    before = expected_access(SPEC, env.log.oracle_mutations(), entry.confirmed_at - 1)
    assert before["p-plateng"] - exp["p-plateng"] == set(CAN[lost])


def test_the_sharepoint_group_removal_really_removes_the_user_and_is_idempotent(
    built: tuple[FakeTenant, Env],
) -> None:
    tenant, env = built
    m = Mutation(
        type=MutationType.GROUP_MEMBER_REMOVED,
        params={"group": "sp-s1-spgroup", "member_kind": "user", "member_ref": "p-plateng"},
    )
    assert apply_mutation(env, m).status == "confirmed"
    assert tenant.sp_groups["ll-sp-s1-spgroup"]["members"] == set()
    again = apply_mutation(env, m)  # already not a member: no second removal call
    assert again.status == "confirmed" and "not a member" in again.note
    assert tenant.calls["sp_group_remove_user"] == 1
    back = apply_mutation(env, m.model_copy(update={"type": MutationType.GROUP_MEMBER_ADDED}))
    assert back.status == "confirmed"
    assert tenant.sp_groups["ll-sp-s1-spgroup"]["members"] == {sp_login(tenant, "p-plateng")}


def test_an_entra_group_inside_a_sharepoint_group_is_refused(
    built: tuple[FakeTenant, Env],
) -> None:
    _, env = built
    m = Mutation(
        type=MutationType.GROUP_MEMBER_ADDED,
        params={"group": "sp-s1-spgroup", "member_kind": "entra_group", "member_ref": "grp-hr"},
    )
    with pytest.raises(GeneratorError, match="not verified"):
        apply_mutation(env, m)
    assert env.log.entries[-1].status == "failed"


def test_teardown_leaves_a_foreign_sharepoint_group_alone(built: tuple[FakeTenant, Env]) -> None:
    tenant, env = built
    tenant.sp.create_sp_group("Team Site Members")
    teardown(make_env(tenant))
    assert set(tenant.sp_groups) == {"Team Site Members"}


def test_a_user_id_that_changes_between_lookups_is_never_reused(
    tmp_path: Path,
) -> None:
    """Regression (live, 2026-10-17): an id cached early became invalid before the grant."""
    del tmp_path
    tenant = FakeTenant()
    tenant.rotate_user_ids = True
    env = make_env(tenant)
    generate(env)
    assert verify(env).ok
    again = generate(make_env(tenant))
    assert again.counts.get("assignments_added", 0) == 0
    apply_mutation(
        env,
        Mutation(
            type=MutationType.GROUP_MEMBER_REMOVED,
            params={"group": "sp-s1-spgroup", "member_kind": "user", "member_ref": "p-plateng"},
        ),
    )
    assert tenant.sp_groups["ll-sp-s1-spgroup"]["members"] == set()
