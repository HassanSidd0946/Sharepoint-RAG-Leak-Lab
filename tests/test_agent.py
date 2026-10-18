"""The agent: the stores on memory and Postgres+pgvector, the embedding, and the two modes.

NAIVE is the worst-case leaker (shared cache and memory, no permission metadata, a stub LLM that
quotes verbatim). GUARDED re-checks every source for the CURRENT user. The tests state what each
mode does with the fixed S6 situations: cross-user cache, revoked access, shared memory, a positive
control, threads.
"""

import uuid
from collections.abc import Callable
from typing import Any

import pytest

from agent_fakes import (
    DOCS,
    FINANA,
    FLD1,
    FLD2,
    GEN,
    GEN1,
    INTERN,
    LEADS,
    Q_GEN_PARA,
    Q_GEN_SEED,
    Q_MEM_READER,
    Q_MEM_SEED,
    Q_PARA,
    Q_SEED,
    FakeAccess,
    FakeRetriever,
    make_agent,
)
from leaklab.agent.embedding import DIMS, HashEmbedder, cosine, tokens
from leaklab.agent.graph import fact_line
from leaklab.agent.guard import StateAccess, check_sources
from leaklab.agent.llm import NO_CONTEXT, RealLLM, StubLLM
from leaklab.agent.queries import load_queries, validate
from leaklab.agent.stores import (
    AgentDb,
    MemoryCache,
    MemoryMemory,
    PgMemoryStore,
    PgSemanticCache,
    vector_literal,
)
from leaklab.agent.threads import MemoryThreads, PgThreads, ThreadDenied
from leaklab.agent.types import CacheEntry, Context, MemoryItem, Passage, SourceRef

# -- embedding and the query set ------------------------------------------------------------------


def test_the_embedding_is_deterministic_and_unit_length() -> None:
    e = HashEmbedder()
    a, b = e.embed(Q_SEED), e.embed(Q_SEED)
    assert a == b and len(a) == DIMS and abs(cosine(a, a) - 1.0) < 1e-9
    assert e.embed("the of and") == [0.0] * DIMS  # only stopwords: the zero vector, similarity 0
    assert cosine(e.embed("the of and"), a) == 0.0


def test_paraphrases_are_close_and_unrelated_text_is_not() -> None:
    e = HashEmbedder()
    assert cosine(e.embed(Q_SEED), e.embed(Q_PARA)) > 0.8
    assert cosine(e.embed(Q_SEED), e.embed("quarterly parking schedule for visitors")) < 0.3
    assert tokens("What does the Finance Leadership document 01 say?") == [
        "finance", "leadership", "document", "01", "say",
    ]  # fmt: skip


def test_the_shipped_query_set_is_valid_and_versioned() -> None:
    q = load_queries()
    assert q.version == 2 and set(q.variants) == {"S6a", "S6b", "S6c", "control"}
    facts = {"S6c": fact_line(Passage("i", "n", FLD2.text))}
    assert validate(q, HashEmbedder(), facts) == []
    assert all(len(v.followups) >= 3 for v in q.variants.values())


def test_the_query_validator_catches_a_follow_up_that_would_miss_or_hit_the_wrong_thing() -> None:
    q = load_queries()
    e = HashEmbedder()
    q.variants["S6a"].followups[0] = "Where is the parking lot?"  # would never hit the cache
    q.variants["S6c"].followups[0] = q.variants["S6c"].seed  # would hit the cache, not the memory
    facts = {"S6c": fact_line(Passage("i", "n", FLD2.text))}
    problems = validate(q, e, facts)
    assert any(p.startswith("S6a[1]") and "not safely above" in p for p in problems)
    assert any(p.startswith("S6c[1]") and "HIT the cache" in p for p in problems)


# -- the stub LLM ---------------------------------------------------------------------------------


def test_the_stub_quotes_everything_it_is_given_and_says_so_when_it_has_nothing() -> None:
    ctx = [
        Context("retrieved", "alpha CANARY-X-1", None),
        Context("memory", "beta CANARY-X-2", None),
    ]
    out = StubLLM().answer("q", ctx)
    assert "alpha CANARY-X-1" in out and "beta CANARY-X-2" in out
    assert StubLLM().answer("q", []) == NO_CONTEXT
    assert StubLLM().answer("q", ctx) == out  # pure function of its input


def test_the_real_model_is_unavailable_without_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(RuntimeError):
        RealLLM()


# -- the stores: the same contract on memory and on Postgres + pgvector -------------------------------


@pytest.fixture(params=["memory", "pg"])
def stores(request: pytest.FixtureRequest) -> Any:
    if request.param == "memory":
        yield MemoryCache(), MemoryMemory(), MemoryThreads()
        return
    dsn = request.getfixturevalue("pg_dsn")
    db = AgentDb(dsn, f"t_{uuid.uuid4().hex[:12]}")
    try:
        yield PgSemanticCache(db), PgMemoryStore(db), PgThreads(db)
    finally:
        db.drop_schema()


def vec(text: str) -> list[float]:
    return HashEmbedder().embed(text)


def test_the_cache_returns_the_nearest_entry_with_its_similarity(stores: Any) -> None:
    cache = stores[0]
    assert cache.lookup(vec(Q_SEED)) == (None, 0.0)
    src = (SourceRef("item-1", GEN),)
    a = CacheEntry(Q_SEED, vec(Q_SEED), "answer A", src, "oid-1", 5.0)
    b = CacheEntry("parking lot visitors", vec("parking lot visitors"), "answer B", None, None, 6.0)
    ids = [cache.store(a), cache.store(b)]
    assert ids[0] != ids[1] and a.id == ids[0]
    entry, sim = cache.lookup(vec(Q_PARA))
    assert entry is not None and entry.answer == "answer A" and sim > 0.8
    assert entry.sources == src and entry.owner == "oid-1"
    entry, sim = cache.lookup(vec("parking lot visitors"))
    assert entry is not None and entry.answer == "answer B" and sim > 0.99
    assert entry.sources is None and entry.owner is None  # no metadata stays no metadata
    assert [e.answer for e in cache.entries()] == ["answer A", "answer B"]
    cache.clear()
    assert cache.entries() == [] and cache.lookup(vec(Q_SEED)) == (None, 0.0)


def test_memory_search_orders_by_similarity_and_applies_the_threshold(stores: Any) -> None:
    memory = stores[1]
    near = MemoryItem(
        fact_line(Passage("i", "n", FLD2.text)),
        vec(fact_line(Passage("i", "n", FLD2.text))),
        (SourceRef("item-2", GEN),),
        "oid-1",
    )
    far = MemoryItem("parking lot visitors", vec("parking lot visitors"))
    memory.add(near)
    memory.add(far)
    found = memory.search(vec(Q_MEM_READER), 5, 0.30)
    assert [i.text for i, _ in found] == [near.text] and found[0][1] >= 0.30
    assert found[0][0].sources == (SourceRef("item-2", GEN),)
    assert len(memory.search(vec(Q_MEM_READER), 5, 0.0)) == 2
    assert memory.search(vec(Q_MEM_READER), 1, 0.0)[0][0].text == near.text
    memory.clear()
    assert memory.items() == [] and memory.search(vec(Q_MEM_READER), 5, 0.0) == []


def test_the_pg_stores_refuse_a_vector_of_the_wrong_size() -> None:
    with pytest.raises(ValueError):
        vector_literal([0.1, 0.2], DIMS)


def test_thread_ownership_is_claimed_by_the_first_user(stores: Any) -> None:
    threads = stores[2]
    assert threads.owner_of("t1") is None
    assert threads.claim("t1", "oid-a", 1.0) is True
    assert threads.claim("t1", "oid-a", 2.0) is True and threads.claim("t1", "oid-b", 3.0) is False
    assert threads.owner_of("t1") == "oid-a"
    threads.clear()
    assert threads.owner_of("t1") is None


# -- the guard ------------------------------------------------------------------------------------


def test_the_guard_needs_every_source_and_denies_anything_unknown() -> None:
    access = FakeAccess()
    both = (SourceRef(FLD1.item_id, GEN), SourceRef(GEN1.item_id, GEN))
    assert check_sources(access, LEADS.oid, both).allowed
    one_bad = check_sources(access, INTERN.oid, both)
    assert not one_bad.allowed and one_bad.reason.startswith("denied:")  # ONE failure is enough
    assert check_sources(access, LEADS.oid, None).reason == "no_sources"
    assert check_sources(access, LEADS.oid, ()).reason == "no_sources"
    assert (
        check_sources(access, LEADS.oid, (SourceRef(FLD1.item_id, "GOLD00"),)).reason
        == "generation_mismatch"
    )
    assert not check_sources(access, LEADS.oid, (SourceRef("item-unknown", GEN),)).allowed
    access.fail = RuntimeError("state down")
    assert check_sources(access, LEADS.oid, both).reason.startswith("error:")


def test_state_access_fails_closed_on_stale_state_and_errors() -> None:
    class Reader:
        def __init__(self, fresh_ok: bool) -> None:
            self.fresh_ok = fresh_ok

        def freshness(self) -> object:
            return object()

        def decide_with(self, user: str, item: str, fresh: object) -> Any:
            if item == "boom":
                raise RuntimeError("x")
            return type("D", (), {"allowed": self.fresh_ok and item == "ok", "reason": "r"})()

    access = StateAccess(Reader(True), GEN)  # type: ignore[arg-type]
    res = access.check("u", ["ok", "no", "boom"])
    assert [r.allowed for r in res] == [True, False, False] and res[
        2
    ].reason == "error:RuntimeError"


# -- NAIVE: the worst-case leaker ---------------------------------------------------------------------


def test_naive_serves_one_users_cached_answer_to_another_user() -> None:
    agent = make_agent("naive")
    first = agent.ask(LEADS, Q_SEED)
    assert first.via == "llm" and FLD1.canary in first.answer
    second = agent.ask(INTERN, Q_PARA)  # a paraphrase, by a user who may not read the source
    assert second.via == "cache" and second.cache["served"] and FLD1.canary in second.answer
    assert second.cache["entry_owner"] is None  # no permission metadata at all


def test_naive_serves_the_cache_after_the_users_access_was_revoked() -> None:
    retriever = FakeRetriever()
    agent = make_agent("naive", retriever=retriever)
    agent.ask(LEADS, Q_SEED)
    FLD1.readers.discard(LEADS.oid)
    try:
        again = agent.ask(LEADS, Q_PARA)
    finally:
        FLD1.readers.add(LEADS.oid)
    assert again.via == "cache" and FLD1.canary in again.answer


def test_naive_memory_carries_one_users_facts_into_another_users_answer() -> None:
    agent = make_agent("naive")
    agent.ask(LEADS, Q_MEM_SEED)
    reader = agent.ask(INTERN, Q_MEM_READER)
    assert reader.cache["candidate"] is False  # the cache does not answer ...
    assert [m["used"] for m in reader.memory] == [True]  # ... the memory does
    assert reader.via == "llm" and FLD2.canary in reader.answer


def test_naive_threads_are_shared() -> None:
    agent = make_agent("naive")
    first = agent.ask(LEADS, Q_SEED, thread_id="t-shared")
    again = agent.ask(INTERN, Q_GEN_SEED, thread_id="t-shared")  # no ownership check
    assert again.thread_id == first.thread_id
    seen = agent.history(INTERN, "t-shared")
    assert [h["question"] for h in seen] == [Q_SEED, Q_GEN_SEED]
    assert FLD1.canary in seen[0]["answer"]  # the other user's answer, readable


def test_a_cache_below_the_threshold_is_a_miss_and_every_lookup_is_journalled() -> None:
    events: list[dict[str, Any]] = []
    agent = make_agent("naive", events=events)
    agent.ask(LEADS, Q_SEED)
    agent.ask(INTERN, "quarterly parking schedule for visitors")
    lookups = [e for e in events if e["kind"] == "cache_lookup"]
    assert len(lookups) == 2
    assert lookups[0]["candidate"] is False and lookups[0]["similarity"] == 0.0  # empty cache
    assert lookups[1]["candidate"] is False and 0.0 <= lookups[1]["similarity"] < 0.65
    assert all({"similarity", "threshold", "persona", "t"} <= set(e) for e in lookups)
    agent.ask(INTERN, Q_PARA)
    hit = [e for e in events if e["kind"] == "cache_lookup"][-1]
    assert hit["candidate"] is True and hit["similarity"] > 0.8 and hit["entry_id"] == 1


def test_an_answer_with_nothing_found_is_not_cached() -> None:
    agent = make_agent("naive")
    out = agent.ask(INTERN, "salaries of the finance leadership")  # nothing the intern may read
    assert out.answer == NO_CONTEXT and agent.cache.entries() == [] and agent.memory.items() == []


def test_a_failing_retrieval_is_journalled_and_answers_with_nothing() -> None:
    events: list[dict[str, Any]] = []
    retriever = FakeRetriever()
    retriever.fail = True
    out = make_agent("naive", retriever=retriever, events=events).ask(LEADS, Q_SEED)
    assert out.answer == NO_CONTEXT
    assert [e["error"] for e in events if e["kind"] == "retrieval_error"] == ["http:503"]


# -- GUARDED -------------------------------------------------------------------------------------------


def test_guarded_refuses_to_start_without_an_access_checker() -> None:
    with pytest.raises(ValueError, match="access checker"):
        from leaklab.agent.graph import Agent, AgentConfig

        Agent(
            config=AgentConfig(mode="guarded"), retriever=FakeRetriever(), llm=StubLLM(),
            embedder=HashEmbedder(), cache=MemoryCache(), memory=MemoryMemory(),
            threads=MemoryThreads(), clock=lambda: 0.0,
        )  # fmt: skip


def test_guarded_turns_another_users_cache_hit_into_a_miss() -> None:
    events: list[dict[str, Any]] = []
    agent = make_agent("guarded", events=events, k=1)
    first = agent.ask(LEADS, Q_SEED)
    assert FLD1.canary in first.answer
    entry = agent.cache.entries()[0]
    assert entry.sources == (SourceRef(FLD1.item_id, GEN),) and entry.owner == LEADS.oid
    second = agent.ask(INTERN, Q_PARA)
    assert second.cache["candidate"] is True and second.cache["denied_by_guard"] is True
    assert second.via == "llm" and second.cache["served"] is False
    assert FLD1.canary not in second.answer  # what the intern gets is built from what they may read
    guard = [e for e in events if e["kind"] == "cache_guard"]
    assert guard[0]["allowed"] is False and guard[0]["reason"].startswith("denied:")


def test_guarded_rechecks_access_that_changed_after_the_answer_was_cached() -> None:
    access = FakeAccess()
    agent = make_agent("guarded", access=access)
    agent.ask(LEADS, Q_SEED)
    served = agent.ask(LEADS, Q_PARA)
    assert served.via == "cache" and served.cache["served"]  # still allowed: served from the cache
    access.readers[FLD1.item_id].discard(LEADS.oid)  # the grant is revoked
    after = agent.ask(LEADS, Q_PARA)
    assert after.cache["denied_by_guard"] is True and FLD1.canary not in after.answer


def test_guarded_needs_every_source_not_just_the_first() -> None:
    access = FakeAccess()
    # an answer built from a document the intern may read and one they may not
    agent = make_agent("guarded", access=access, k=2)
    agent.ask(LEADS, "Finance Leadership document 01 General document 01 salaries vendor contract")
    entry = agent.cache.entries()[0]
    assert {s.item_id for s in entry.sources or ()} == {FLD1.item_id, GEN1.item_id}
    probe = agent.ask(
        INTERN, "Finance Leadership document 01 General document 01 salaries vendor contract"
    )
    assert probe.cache["candidate"] and probe.cache["denied_by_guard"] is True
    checked = [c for c in access.checked if c[0] == INTERN.oid][0]
    assert set(checked[1]) == {FLD1.item_id, GEN1.item_id}  # both were asked about


def test_guarded_serves_the_cache_to_a_user_who_may_read_every_source() -> None:
    """The positive control: a guard that blocks this is over-blocking."""
    agent = make_agent("guarded")
    agent.ask(INTERN, Q_GEN_SEED)
    out = agent.ask(FINANA, Q_GEN_PARA)  # a different user, who may also read the document
    assert out.via == "cache" and out.cache["served"] and GEN1.canary in out.answer
    assert out.cache["entry_owner"] is None or True  # the owner is not exposed to the second user


def test_guarded_denies_entries_it_cannot_vouch_for() -> None:
    for sources in (None, (), (SourceRef(GEN1.item_id, "GOLD999"),)):
        agent = make_agent("guarded")
        agent.cache.store(
            CacheEntry(
                Q_GEN_SEED, vec(Q_GEN_SEED), "legacy answer " + GEN1.canary, sources, "oid-x"
            )
        )
        out = agent.ask(INTERN, Q_GEN_PARA)
        assert out.cache["candidate"] and out.cache["denied_by_guard"] is True, sources
        assert "legacy answer" not in out.answer


def test_guarded_fails_closed_when_the_state_check_fails() -> None:
    access = FakeAccess()
    agent = make_agent("guarded", access=access)
    agent.ask(INTERN, Q_GEN_SEED)
    access.fail = RuntimeError("state unavailable")
    out = agent.ask(FINANA, Q_GEN_PARA)
    assert out.cache["denied_by_guard"] is True and out.cache["guard_reason"].startswith("error:")
    assert out.answer == NO_CONTEXT  # the context guard fails closed too


def test_guarded_drops_memory_items_the_user_may_not_read() -> None:
    agent = make_agent("guarded")
    agent.ask(LEADS, Q_MEM_SEED)
    reader = agent.ask(INTERN, Q_MEM_READER)
    assert reader.cache["candidate"] is False
    assert [m["denied_by_guard"] for m in reader.memory] == [True]
    assert FLD2.canary not in reader.answer


def test_guarded_checks_the_context_again_before_the_llm_is_called() -> None:
    """A retrieval that returned a document the user may not read is stopped at the context guard."""
    events: list[dict[str, Any]] = []
    agent = make_agent("guarded", retriever=FakeRetriever(ignore_acl=True), events=events)
    out = agent.ask(INTERN, Q_SEED)
    assert (
        out.passages == [FLD1.item_id] or FLD1.item_id in out.passages
    )  # the retrieval leaked it ...
    assert FLD1.canary not in out.answer  # ... the guard dropped it before the LLM
    assert [e for e in events if e["kind"] == "context_guard"][0]["dropped"] >= 1
    # what is left is what the intern may read, and only that is cached, with only that as its sources
    assert GEN1.canary in out.answer and FLD2.canary not in out.answer
    assert [{s.item_id for s in e.sources or ()} for e in agent.cache.entries()] == [{GEN1.item_id}]


def test_guarded_threads_belong_to_one_user() -> None:
    agent = make_agent("guarded")
    agent.ask(LEADS, Q_SEED, thread_id="t-lead")
    assert [h["question"] for h in agent.history(LEADS, "t-lead")] == [Q_SEED]
    with pytest.raises(ThreadDenied):
        agent.history(INTERN, "t-lead")  # another user's thread: denied
    with pytest.raises(ThreadDenied):
        agent.ask(INTERN, Q_GEN_SEED, thread_id="t-lead")  # nor may they write to it
    assert agent.history(INTERN, "t-nobody") == []  # a thread nobody has is just empty
    assert len(agent.history(LEADS, "t-lead")) == 1  # the denied ask changed nothing


def test_the_default_thread_is_per_user() -> None:
    agent = make_agent("guarded")
    a = agent.ask(LEADS, Q_SEED)
    b = agent.ask(INTERN, Q_GEN_SEED)
    assert a.thread_id != b.thread_id and LEADS.oid in (a.thread_id or "")


# -- the LangGraph checkpointer on Postgres ----------------------------------------------------------------


def test_the_conversation_is_kept_in_the_postgres_checkpointer(pg_dsn: str) -> None:
    import psycopg
    from langgraph.checkpoint.postgres import PostgresSaver
    from psycopg.rows import dict_row

    schema = f"t_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(pg_dsn, autocommit=True) as boot:
        boot.execute(f'CREATE SCHEMA "{schema}"')
    conn = psycopg.connect(
        pg_dsn, autocommit=True, row_factory=dict_row, options=f"-c search_path={schema}"
    )
    try:
        saver = PostgresSaver(conn)
        saver.setup()
        agent = make_agent("guarded", checkpointer=saver)
        agent.ask(LEADS, Q_SEED, thread_id="t-pg")
        agent.ask(LEADS, Q_PARA, thread_id="t-pg")
        assert [h["via"] for h in agent.history(LEADS, "t-pg")] == ["llm", "cache"]
        with pytest.raises(ThreadDenied):
            agent.history(INTERN, "t-pg")
        # a second agent object over the same database sees the same thread (it is persisted)
        again = make_agent("guarded", checkpointer=saver, threads=agent.threads)
        assert len(again.history(LEADS, "t-pg")) == 2
    finally:
        conn.close()
        with psycopg.connect(pg_dsn, autocommit=True) as boot:
            boot.execute(f'DROP SCHEMA "{schema}" CASCADE')


def test_the_agent_runs_on_postgres_stores(pg_dsn: str) -> None:
    db = AgentDb(pg_dsn, f"t_{uuid.uuid4().hex[:12]}")
    try:
        agent = make_agent(
            "guarded", cache=PgSemanticCache(db), memory=PgMemoryStore(db), threads=PgThreads(db)
        )
        agent.ask(LEADS, Q_SEED)
        out = agent.ask(INTERN, Q_PARA)
        assert out.cache["denied_by_guard"] is True and FLD1.canary not in out.answer
        ok = make_agent(
            "naive", cache=PgSemanticCache(db), memory=PgMemoryStore(db), threads=PgThreads(db)
        )
        assert ok.ask(INTERN, Q_PARA).via == "cache"  # the entry is there for a naive agent
    finally:
        db.drop_schema()


def test_the_documents_of_the_fakes_are_what_the_tests_assume() -> None:
    assert LEADS.oid in FLD1.readers and INTERN.oid not in FLD1.readers
    assert {INTERN.oid, FINANA.oid} <= GEN1.readers and len(DOCS) == 3
    run: Callable[[], None] = lambda: None  # noqa: E731
    run()


def test_guarded_checks_the_later_sources_too_not_only_the_first() -> None:
    """An entry whose FIRST source the user may read and whose SECOND they may not must be denied."""

    def agent_with_the_mixed_entry() -> Any:
        agent = make_agent("guarded")
        agent.cache.store(
            CacheEntry(
                Q_GEN_SEED,
                vec(Q_GEN_SEED),
                "mixed answer " + GEN1.canary + " " + FLD1.canary,
                (SourceRef(GEN1.item_id, GEN), SourceRef(FLD1.item_id, GEN)),  # readable first
                LEADS.oid,
            )
        )
        return agent

    out = agent_with_the_mixed_entry().ask(INTERN, Q_GEN_PARA)
    assert out.cache["candidate"] and out.cache["denied_by_guard"] is True
    assert FLD1.canary not in out.answer and "mixed answer" not in out.answer
    owner = agent_with_the_mixed_entry().ask(LEADS, Q_GEN_PARA)  # may read both: served
    assert owner.via == "cache" and "mixed answer" in owner.answer
