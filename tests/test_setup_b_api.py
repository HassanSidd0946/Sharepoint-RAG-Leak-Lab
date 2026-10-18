"""Setup B's API: tokens, the hash-chained log, and /query /health /explain /metrics end to end over
the fake tenant, with the oracle (tests only) as the judge of what each persona may be served."""

import threading
import uuid
from collections.abc import Callable

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from leaklab.oracle import expected_access
from leaklab.setup_b.api.decisionlog import (
    GENESIS,
    Entry,
    MemoryDecisionLog,
    PgDecisionLog,
    Record,
    compute_hash,
    verify_records,
)
from leaklab.setup_b.api.search import IndexAcl, IndexUnavailable
from leaklab.setup_b.api.tokens import InvalidToken, Principal, TokenValidator
from leaklab.setup_b.state.repository import StateRepository
from runner_fakes import SPEC
from setup_b_api_world import ALL, AUDIENCE, TENANT, Api, Keys, P, operator
from setup_b_world import World

Factory = Callable[[], StateRepository]
PERSONAS = [u.id for u in SPEC.users if u.persona]


def validator(keys: Keys) -> TokenValidator:
    return TokenValidator(tenant_id=TENANT, audience=AUDIENCE, keys=keys)


# -- tokens ---------------------------------------------------------------------------------------


def test_a_good_token_gives_the_user() -> None:
    keys = Keys()
    who = validator(keys).validate(keys.token("AAAA-1111", "Ann@Example.Test"))
    assert who == Principal(oid="aaaa-1111", upn="ann@example.test")


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"aud": "https://example.invalid"}, "audience"),
        ({"tid": "99999999-0000-0000-0000-000000000000"}, "tenant"),
        ({"iss": "https://sts.windows.net/99999999-0000-0000-0000-000000000000/"}, "issuer"),
        ({"exp": 1}, "expired"),
        ({"scp": None}, "not_a_user_token"),
        ({"idtyp": "app"}, "not_a_user_token"),
    ],
)
def test_bad_claims_are_refused(overrides: dict[str, object], code: str) -> None:
    keys = Keys()
    with pytest.raises(InvalidToken) as err:
        validator(keys).validate(keys.token("oid-1", **overrides))
    assert err.value.code == code


def test_a_token_without_a_user_identity_is_refused() -> None:
    keys = Keys()
    for token in (keys.token(None), keys.token("oid-1", None), keys.token("")):
        with pytest.raises(InvalidToken) as err:
            validator(keys).validate(token)
        assert err.value.code == "no_user_identity"


def test_a_token_signed_by_another_key_is_refused() -> None:
    keys = Keys()
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(InvalidToken) as err:
        validator(keys).validate(keys.token("oid-1", key=other))
    assert err.value.code == "invalid"


def test_unknown_key_id_garbage_and_downgraded_algorithms_are_refused() -> None:
    keys = Keys()
    v = validator(keys)
    with pytest.raises(InvalidToken) as unknown:
        v.validate(keys.token("oid-1", kid="other-kid"))
    assert unknown.value.code == "unknown_key"
    for garbage in ("", "abc", "a.b.c"):
        with pytest.raises(InvalidToken):
            v.validate(garbage)
    unsigned = jwt.encode({"oid": "x", "aud": AUDIENCE}, None, algorithm="none")
    with pytest.raises(InvalidToken) as none_alg:
        v.validate(unsigned)
    assert none_alg.value.code == "algorithm"
    hs = jwt.encode({"oid": "x"}, "k" * 40, algorithm="HS256", headers={"kid": keys.kid})
    with pytest.raises(InvalidToken) as hs_alg:
        v.validate(hs)
    assert hs_alg.value.code == "algorithm"


def test_the_v2_issuer_is_accepted_too() -> None:
    keys = Keys()
    token = keys.token("oid-1", iss=f"https://login.microsoftonline.com/{TENANT}/v2.0")
    assert validator(keys).validate(token).oid == "oid-1"


# -- the decision log -----------------------------------------------------------------------------


def entry(n: int, allowed: bool = True) -> Entry:
    return Entry(float(n), "decision", f"user-{n}", f"doc-{n}", allowed, "allowed", "req", {"n": n})


def test_the_chain_verifies_and_any_change_breaks_it() -> None:
    log = MemoryDecisionLog()
    log.append([entry(i) for i in range(1, 6)])
    assert log.verify().ok and log.verify().checked == 5
    records = log.records()
    assert records[0].prev_hash == GENESIS
    flipped = [*records]
    flipped[2] = Record(3, entry(3, allowed=False), records[2].prev_hash, records[2].hash)
    assert verify_records(flipped).first_bad_seq == 3  # a changed decision
    assert verify_records(records[:2] + records[3:]).first_bad_seq == 4  # a deleted record
    assert verify_records([records[1], records[0], *records[2:]]).ok is False  # reordered
    forged = Record(
        3,
        entry(3, allowed=False),
        records[2].prev_hash,
        compute_hash(records[2].prev_hash, 3, entry(3, False)),
    )
    assert (
        verify_records([*records[:2], forged, *records[3:]]).first_bad_seq == 4
    )  # rehash breaks the next


def test_concurrent_appends_still_make_one_chain(pg_dsn: str) -> None:
    for log in (MemoryDecisionLog(), PgDecisionLog(pg_dsn, f"t_{uuid.uuid4().hex[:12]}")):
        threads = [
            threading.Thread(
                target=lambda i=i, log=log: log.append([entry(i * 10 + j) for j in range(5)])
            )
            for i in range(6)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        v = log.verify()
        assert v.ok and v.checked == 30
        if isinstance(log, PgDecisionLog):
            log.drop_schema()


def test_postgres_log_detects_a_tampered_row(pg_dsn: str) -> None:
    import psycopg

    schema = f"t_{uuid.uuid4().hex[:12]}"
    log = PgDecisionLog(pg_dsn, schema)
    try:
        log.append([entry(i) for i in range(1, 5)])
        assert log.verify().ok
        with psycopg.connect(pg_dsn, autocommit=True) as other:
            other.execute(f'UPDATE "{schema}".decisions SET allowed = NOT allowed WHERE seq = 2')
        assert log.verify().first_bad_seq == 2
    finally:
        log.drop_schema()


# -- /query ---------------------------------------------------------------------------------------


def test_every_persona_is_served_exactly_what_the_oracle_expects(make_repo: Factory) -> None:
    api = Api(World(make_repo()))
    api.world.sync()
    expected = expected_access(SPEC, api.world.env.log.oracle_mutations(), float("inf"))
    for persona in PERSONAS:
        assert api.served_canaries(persona) == expected[persona], persona
    assert api.log.verify().ok


def test_the_answer_follows_a_revocation_after_the_next_poll(make_repo: Factory) -> None:
    api = Api(World(make_repo()))
    api.world.sync()
    canary = P + "S1D-01"
    served = lambda: {c for c in api.served_canaries("p-plateng") if c.startswith(canary)}  # noqa: E731
    assert served(), "p-plateng starts with the direct grant"
    api.world.apply(
        "grant_removed", target="S1-Direct", principal_kind="user", principal_ref="p-plateng"
    )
    api.world.sync()
    api.world.sync()
    assert not served()


def test_paging_goes_on_until_k_results_are_allowed(make_repo: Factory) -> None:
    api = Api(World(make_repo()), page_size=lambda k: 4)
    api.world.sync()
    stats = api.query("p-ceo", k=3, debug=True, measure_recall=True)["stats"]
    assert stats["returned"] == 3 and stats["shortfall"] == 0 and stats["pages"] > 1
    assert stats["allowed"] >= 3 and stats["capped"] is False and stats["exhausted"] is False
    # every page asks for the next hits: no hit is fetched twice
    assert [q[2] for q in api.index.queries[: stats["pages"]]] == [
        4 * i for i in range(stats["pages"])
    ]
    assert len(set(stats["doc_ids"])) == len(stats["doc_ids"]) == stats["fetched"]


def test_paging_stops_at_the_first_page_when_it_already_holds_k_allowed_results(
    make_repo: Factory,
) -> None:
    api = Api(World(make_repo()))
    api.world.sync()
    stats = api.query("p-plateng", k=3, debug=True)["stats"]  # reads most of the corpus
    assert stats["pages"] == 1 and stats["returned"] == 3 and len(api.index.queries) == 1


def test_paging_stops_when_the_index_is_exhausted(make_repo: Factory) -> None:
    api = Api(World(make_repo()))
    api.world.sync()
    stats = api.query("p-intern", k=50, debug=True, measure_recall=True)["stats"]
    assert stats["exhausted"] is True and stats["capped"] is False
    assert stats["returned"] == stats["allowed_total"] < 50  # everything readable was found
    assert stats["fetched"] == len(ALL)


def test_a_page_cap_limits_the_work_and_the_shortfall_is_reported(make_repo: Factory) -> None:
    api = Api(World(make_repo()), max_pages=1, page_size=lambda k: k)
    api.world.sync()
    stats = api.query("p-ceo", k=3, debug=True, measure_recall=True)["stats"]
    assert stats["pages"] == 1 and stats["capped"] is True and stats["exhausted"] is False
    assert stats["returned"] < 3 <= stats["allowed_total"]  # readable documents exist, not reached
    assert stats["shortfall"] == 3 - stats["returned"] > 0


def test_the_old_fixed_window_is_the_same_paging_with_one_page_of_5k(make_repo: Factory) -> None:
    """page_size = 5k and one page IS the old over-fetch: the baseline the recall is compared with."""
    api = Api(World(make_repo()), page_size=lambda k: 5 * k, max_pages=1)
    api.world.sync()
    stats = api.query("p-ceo", k=3, debug=True, measure_recall=True)["stats"]
    assert stats["page_size"] == 15 and stats["fetched"] == 15 and stats["pages"] == 1
    assert stats["returned"] < 3 <= stats["allowed_total"]  # the cost the adaptive paging removes
    assert api.index.queries[0] == ("CANARY", 15, 0)


def test_the_default_paging_returns_k_where_the_fixed_window_could_not(make_repo: Factory) -> None:
    fixed = Api(World(make_repo()), page_size=lambda k: 5 * k, max_pages=1)
    adaptive = Api(World(make_repo()))
    for api in (fixed, adaptive):
        api.world.sync()
    assert len(fixed.query("p-ceo", k=3)["results"]) < 3
    assert len(adaptive.query("p-ceo", k=3)["results"]) == 3


def test_the_pages_are_in_the_decision_log_and_only_decided_hits_are_logged(
    make_repo: Factory,
) -> None:
    api = Api(World(make_repo()), page_size=lambda k: 4)
    api.world.sync()
    stats = api.query("p-ceo", k=3, debug=True)["stats"]
    summary = next(r for r in api.log.records() if r.entry.kind == "query").entry
    assert summary.detail["pages"] == stats["pages"] > 1
    decided = [r for r in api.log.records() if r.entry.kind == "decision"]
    assert len(decided) == stats["fetched"] < len(ALL)


def test_never_more_than_k_results(make_repo: Factory) -> None:
    api = Api(World(make_repo()), page_size=lambda k: 100)  # one page as big as the index
    api.world.sync()
    assert len(api.query("p-ceo", k=3)["results"]) == 3


def test_debug_stats_need_the_server_to_allow_them(make_repo: Factory) -> None:
    api = Api(World(make_repo()), allow_debug=False)
    api.world.sync()
    assert "stats" not in api.query("p-ceo", debug=True, measure_recall=True)


def test_a_stale_membership_state_denies_group_grants_and_reports_degraded(
    make_repo: Factory,
) -> None:
    w = World(make_repo(), max_age_s=12.0)
    api = Api(w)
    w.sync()
    before = api.served_canaries("p-plateng")
    assert any(c.startswith(P + "GEN") for c in before)  # General: via grp-all-staff
    for _ in range(3):
        w.group_src.fail_next = ConnectionError("down")
        w.clock.sleep(5.0)
        w.membership.poll_once()
        w.acl.poll_once()
    after = api.served_canaries("p-plateng")
    assert not any(c.startswith(P + "GEN") for c in after)  # every group-based grant is denied
    assert any(c.startswith(P + "S1D") for c in after)  # a grant naming the user still stands
    assert api.query("p-plateng")["degraded"] is True
    health = api.client.get("/health")
    assert health.status_code == 200 and health.json()["status"] == "degraded"
    w.membership.poll_once()
    assert api.client.get("/health").json()["status"] == "ok"
    assert api.served_canaries("p-plateng") == before


def test_a_stale_acl_state_denies_everything(make_repo: Factory) -> None:
    w = World(make_repo(), max_age_s=12.0)
    api = Api(w)
    w.sync()
    for _ in range(3):
        w.drive.fail_next = ConnectionError("down")
        w.clock.sleep(5.0)
        w.membership.poll_once()
        w.acl.poll_once()
    assert api.served_canaries("p-ceo") == set()
    assert api.client.get("/health").json()["status"] == "degraded"


def test_nothing_polled_yet_denies_everything(make_repo: Factory) -> None:
    api = Api(World(make_repo()))
    assert api.served_canaries("p-ceo") == set()
    assert api.client.get("/health").json()["status"] == "degraded"


def test_an_unknown_principal_gets_nothing(make_repo: Factory) -> None:
    api = Api(World(make_repo()))
    api.world.sync()
    token = api.keys.token("ffffffff-0000-0000-0000-000000000000", "ghost@example.test")
    resp = api.client.post("/query", json={"persona_token": token, "query": "CANARY", "k": 50})
    assert resp.status_code == 200 and resp.json()["results"] == []
    reasons = {r.entry.reason for r in api.log.records() if r.entry.kind == "decision"}
    assert reasons == {"unknown_principal"}


def test_a_bad_token_is_a_401_and_touches_nothing(make_repo: Factory) -> None:
    api = Api(World(make_repo()))
    api.world.sync()
    for token in ("garbage", api.keys.token("oid-1", aud="https://example.invalid")):
        resp = api.client.post("/query", json={"persona_token": token, "query": "CANARY"})
        assert resp.status_code == 401 and set(resp.json()) == {"error"}
    assert api.index.queries == [] and api.log.records() == []


def test_a_malformed_request_is_a_422(make_repo: Factory) -> None:
    api = Api(World(make_repo()))
    token = api.token_for("p-ceo")
    for body in (
        {"persona_token": token},
        {"persona_token": token, "query": "x", "k": 0},
        {"persona_token": token, "query": "x", "k": 51},
        {"persona_token": token, "query": "x", "extra": 1},
        {"persona_token": token, "query": ""},
    ):
        assert api.client.post("/query", json=body).status_code == 422


def test_an_index_failure_is_a_503_with_no_results(make_repo: Factory) -> None:
    api = Api(World(make_repo()))
    api.world.sync()
    api.index.fail = IndexUnavailable("http:500")
    resp = api.client.post(
        "/query", json={"persona_token": api.token_for("p-ceo"), "query": "CANARY"}
    )
    assert resp.status_code == 503 and "results" not in resp.json()


def test_if_the_log_cannot_be_written_nothing_is_returned(make_repo: Factory) -> None:
    class BrokenLog(MemoryDecisionLog):
        def append(self, entries: object) -> None:  # type: ignore[override]
            raise RuntimeError("disk full")

    api = Api(World(make_repo()), log=BrokenLog())
    api.world.sync()
    resp = api.client.post(
        "/query", json={"persona_token": api.token_for("p-ceo"), "query": "CANARY"}
    )
    assert resp.status_code == 503 and resp.json() == {"error": "decision_log_unavailable"}


def test_the_log_holds_decisions_but_never_the_token_the_query_or_content(
    make_repo: Factory,
) -> None:
    api = Api(World(make_repo()))
    api.world.sync()
    token = api.token_for("p-intern")
    api.client.post("/query", json={"persona_token": token, "query": "secret search words", "k": 5})
    dump = repr(api.log.records())
    assert token not in dump and "secret search words" not in dump and "content of" not in dump
    summary = [r for r in api.log.records() if r.entry.kind == "query"][0].entry
    assert len(summary.detail["query_sha256"]) == 64
    assert summary.detail["fetched"] >= summary.detail["returned"]


# -- /explain and /metrics -------------------------------------------------------------------------


def test_explain_and_metrics_need_the_operator_key(make_repo: Factory) -> None:
    api = Api(World(make_repo()))
    api.world.sync()
    doc = api.index.docs[0].doc_id
    assert api.client.get("/explain", params={"user": "x", "doc": doc}).status_code == 401
    wrong = {"X-Operator-Key": "nope"}
    assert api.client.get("/metrics", headers=wrong).status_code == 401
    off = Api(World(make_repo()), operator_key="")
    assert off.client.get("/metrics", headers=operator()).status_code == 403


def test_explain_shows_the_path_and_the_index_acl_only_for_comparison(make_repo: Factory) -> None:
    w = World(make_repo())
    api = Api(w)
    w.sync()
    doc = next(
        h.doc_id for h in api.index.docs if api.index.canary_of[h.doc_id].startswith(P + "GEN")
    )
    # the index claims p-intern may NOT read it; the state says they may: the state wins
    api.index.stored[doc] = IndexAcl(user_ids=[], group_ids=["nobody"])
    uid = w.uid("p-intern")
    out = api.client.get("/explain", params={"user": uid, "doc": doc}, headers=operator()).json()
    assert out["decision"]["allowed"] is True
    assert out["path"].startswith("ALLOW") and "->" in out["path"]
    assert any(g["matches_user"] for g in out["grants"])
    assert out["index_acl_snapshot"]["group_ids"] == ["nobody"]
    assert "never used for decisions" in out["index_acl_snapshot"]["note"]
    # and the other way round: the index lists the user, the state denies
    board = next(
        h.doc_id for h in api.index.docs if api.index.canary_of[h.doc_id].startswith(P + "BRD")
    )
    api.index.stored[board] = IndexAcl(user_ids=[uid], group_ids=[])
    denied = api.client.get(
        "/explain", params={"user": uid, "doc": board}, headers=operator()
    ).json()
    assert denied["decision"]["allowed"] is False and denied["path"].startswith("DENY")
    assert board not in {r["doc_id"] for r in api.query("p-intern")["results"]}


def test_explain_accepts_a_upn_and_is_logged(make_repo: Factory) -> None:
    w = World(make_repo())
    api = Api(w)
    w.sync()
    upn = w.repo.upn_of(w.uid("p-ceo"))
    assert upn
    doc = api.index.docs[0].doc_id
    out = api.client.get(
        "/explain", params={"user": upn.upper(), "doc": doc}, headers=operator()
    ).json()
    assert out["user"]["id"] == w.uid("p-ceo")
    assert [r.entry.kind for r in api.log.records()] == ["explain"]


def test_metrics_report_lag_and_decisions(make_repo: Factory) -> None:
    api = Api(World(make_repo()))
    api.world.sync()
    api.query("p-intern")
    m = api.client.get("/metrics", headers=operator()).json()
    assert m["poller_lag_s"]["membership"] is not None and m["degraded"] is False
    assert m["counters"]["queries"] == 1
    assert m["counters"]["decisions_allow"] > 0 and m["counters"]["decisions_deny"] > 0
    assert m["decision_log"]["last_seq"] > 0
    assert len(ALL) >= m["recent_queries"]["mean_fetched"]


def test_unknown_paths_do_not_create_unbounded_counters(make_repo: Factory) -> None:
    api = Api(World(make_repo()))
    for i in range(5):
        api.client.get(f"/nope/{i}")
    m = api.client.get("/metrics", headers=operator()).json()
    assert [k for k in m["counters"] if k.startswith("http:")].count("http:other:404") == 1


def test_the_recall_tool_totals_returned_possible_and_pages() -> None:
    from leaklab.setup_b.recall import Sample, summarize

    def sample(mode: str, k: int, returned: int, possible: int, pages: int, capped: bool = False):  # type: ignore[no-untyped-def]
        return Sample(
            mode=mode, persona="p", k=k, returned=returned, possible=possible, pages=pages,
            fetched=pages * 10, capped=capped, exhausted=False,
        )  # fmt: skip

    summary = summarize(
        [
            sample("adaptive", 3, 3, 3, 2),
            sample("adaptive", 3, 3, 3, 1),
            sample("fixed_5k", 3, 1, 3, 1),
            sample("fixed_5k", 3, 3, 3, 1),
            sample("adaptive", 10, 4, 6, 10, capped=True),
        ]
    )
    assert summary["adaptive"]["3"] == {
        "samples": 2, "returned": 6, "possible": 6, "lost": 0, "short": 0,
        "pages_sum": 3, "pages_max": 2, "capped": 0,
    }  # fmt: skip
    assert summary["fixed_5k"]["3"]["lost"] == 2 and summary["fixed_5k"]["3"]["short"] == 1
    assert summary["adaptive"]["10"]["lost"] == 2 and summary["adaptive"]["10"]["capped"] == 1
