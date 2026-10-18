"""Run S6 against the real test tenant: `python -m leaklab.agent s6 run`.

Setup, in order (the first four are what run_once does for the mutation runner):
  1. tear the tenant down and generate it again (a NEW generation: new canaries), unless --no-fresh;
  2. ensure_ready (verify, generate if needed), then delete the documents of earlier generations from
     both indexes;
  3. Setup B: a scratch Postgres schema for its state, the two pollers, the API on 127.0.0.1; wait
     until /ready conditions hold (state/readiness.py);
  4. Setup A: the ACL-filtered index queried with each persona's own token;
  5. the agent database (Postgres + pgvector: embedded by default, or LEAKLAB_AGENT_PG_DSN), one schema
     per arm, and one LangGraph Postgres checkpointer per arm;
  6. the S6 driver, journalling into <dir>/s6_<UTC>.jsonl.
The LLM is ALWAYS the StubLLM here. There is no option to change it: a real model is for
`explore` only and never produces benchmark numbers.
"""

import threading
import time
from collections.abc import Callable
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg.rows import dict_row

from leaklab.agent.embedding import HashEmbedder
from leaklab.agent.graph import Agent, AgentConfig
from leaklab.agent.guard import StateAccess
from leaklab.agent.llm import LLM, StubLLM
from leaklab.agent.queries import QuerySet, load_queries, validate
from leaklab.agent.retrieval import SetupARetriever, SetupBRetriever
from leaklab.agent.s6.driver import VARIANT_ORDER, ArmRuntime, DriverConfig, S6Driver
from leaklab.agent.stores import AgentDb, PgMemoryStore, PgSemanticCache
from leaklab.agent.threads import PgThreads
from leaklab.agent.types import Passage, UserCtx
from leaklab.auth import get_persona_token
from leaklab.config import get_settings
from leaklab.generator import Env, LogEntry, apply_mutation, teardown
from leaklab.generator.cli import open_env
from leaklab.oracle import Mutation, MutationType
from leaklab.runner.journal import Journal
from leaklab.runner.model import RunnerError
from leaklab.runner.run import SETTLE_AFTER_TEARDOWN_S, ensure_ready, spec_sha256
from leaklab.runner.scenarios import scenario_mutations
from leaklab.runner.setup0 import TokenCache
from leaklab.runner.tenant import canary_locations, clean_indexes_for_run, saved_endpoint
from leaklab.setup_a.client import SEARCH_SCOPE, HttpSearchPort
from leaklab.setup_a.system import TOKEN_MAX_AGE_S
from leaklab.setup_b.api.app import create_app
from leaklab.setup_b.api.server import LocalServer, build_service
from leaklab.setup_b.state.run import open_stores, start_pollers
from leaklab.spec import TenantSpec

READY_CAP_S = 300.0
INVERSE = {
    MutationType.GRANT_REMOVED: MutationType.GRANT_ADDED,
    MutationType.GROUP_MEMBER_REMOVED: MutationType.GROUP_MEMBER_ADDED,
}


def inverse(m: Mutation) -> Mutation:
    """The mutation that undoes `m` (a removed grant is added again, as a read grant)."""
    kind = INVERSE.get(m.type)
    if kind is None:
        raise RunnerError(f"no inverse known for {m.type.value}")
    params = dict(m.params)
    if m.type is MutationType.GRANT_REMOVED:
        params["role"] = "read"  # the S1-Direct grant is a read grant (tenant_spec.yaml)
    return Mutation(type=kind, params=params)


class TenantOps:
    """revoke / restore of a scenario's mutation on the real tenant (generator.apply_mutation)."""

    def __init__(self, env: Env, spec: TenantSpec) -> None:
        self._env, self._spec = env, spec

    def revoke(self, scenario: str) -> LogEntry:
        [m] = scenario_mutations(next(s for s in self._spec.scenarios if s.id == scenario))
        return apply_mutation(self._env, m)

    def restore(self, scenario: str) -> LogEntry:
        [m] = scenario_mutations(next(s for s in self._spec.scenarios if s.id == scenario))
        return apply_mutation(self._env, inverse(m))


def embedded_agent_dsn(folder: Path) -> tuple[str, Callable[[], None]]:
    """A Postgres with pgvector for the agent: the embedded server of `pgserver` (data in `folder`)."""
    import pgserver

    folder.mkdir(parents=True, exist_ok=True)
    server = pgserver.get_server(folder, cleanup_mode="stop")  # type: ignore[attr-defined]
    return str(server.get_uri()), server.cleanup


def make_arm_factory(
    *,
    dsn: str,
    stack: ExitStack,
    llm: LLM,
    embedder: HashEmbedder,
    clock: Callable[[], float],
    spec: TenantSpec,
    search: HttpSearchPort,
    b_url: str,
    tokens: Callable[[str], str],
    access: StateAccess,
    cache_threshold: float,
    memory_threshold: float,
    run_tag: str,
) -> Callable[[str, str, str, Callable[[dict[str, Any]], None]], ArmRuntime]:
    def factory(
        name: str, mode: str, retrieval: str, journal: Callable[[dict[str, Any]], None]
    ) -> ArmRuntime:
        schema = f"s6_{run_tag}_{name}"
        db = AgentDb(dsn, schema, embedder.dims)
        stack.callback(db.drop_schema)
        conn = psycopg.connect(
            dsn, autocommit=True, row_factory=dict_row, options=f"-c search_path={schema},public"
        )
        stack.callback(conn.close)
        saver = PostgresSaver(conn)
        saver.setup()
        cache, memory, threads = PgSemanticCache(db), PgMemoryStore(db), PgThreads(db)
        retriever = (
            SetupARetriever(search, tokens)
            if retrieval == "setup_a"
            else SetupBRetriever(b_url, tokens)
        )
        agent = Agent(
            config=AgentConfig(
                mode=mode,  # type: ignore[arg-type]
                generation=spec.naming.generation,
                cache_threshold=cache_threshold,
                memory_threshold=memory_threshold,
            ),
            retriever=retriever,
            llm=llm,
            embedder=embedder,
            cache=cache,
            memory=memory,
            threads=threads,
            clock=clock,
            access=access if mode == "guarded" else None,
            checkpointer=saver,
            journal=journal,
        )
        return ArmRuntime(name, agent, cache, memory, threads, retriever)

    return factory


def run_s6_live(
    *,
    spec_path: Path,
    out_dir: Path,
    n: int = 3,
    settle_s: float = 30.0,
    fresh: bool = True,
    agent_dsn: str | None = None,
    log: Callable[[str], object] = lambda text: None,
    exploratory: bool = False,
    llm: LLM | None = None,
    variants: tuple[str, ...] = VARIANT_ORDER,
) -> Path:
    """Run the S6 set on the live tenant; returns the journal path.

    `llm` (a real model) is accepted ONLY with exploratory=True: the journal is then named s6x_*,
    which the benchmark report does not read, and carries "exploratory": true. Benchmark runs always
    use the StubLLM.
    """
    if llm is not None and not exploratory:
        raise RunnerError(
            "a real LLM is for exploratory runs only: it never produces benchmark numbers"
        )
    llm = llm or StubLLM()
    queries: QuerySet = load_queries()
    embedder = HashEmbedder()
    from leaklab.agent.graph import fact_line

    fact = fact_line(
        Passage(
            "i", "n", "Finance/Leadership document 02\nx\nTopic: incident review\nReference code: C"
        )
    )
    problems = validate(queries, embedder, {"S6c": fact})
    if problems:
        raise RunnerError("the query set is not valid: " + "; ".join(problems))
    if fresh:
        with open_env(spec_path) as env0:
            log("tearing the tenant down ...")
            teardown(env0)
            env0.sleep(SETTLE_AFTER_TEARDOWN_S)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = out_dir / f"{'s6x' if exploratory else 's6'}_{stamp}.jsonl"
    stack = ExitStack()
    with stack, open_env(spec_path) as env:
        tenant = ensure_ready(env)
        spec = env.spec  # the live generation's canaries
        canaries = list(canary_locations(env))
        cleanup = clean_indexes_for_run(env, canaries)
        log(f"generation {env.generation}; index cleanup: {cleanup.get('error') or 'done'}")
        users = {u.id: UserCtx(u.id, env.user_id(u.id), u.upn) for u in env.plan.users if u.persona}
        dsn = agent_dsn
        if dsn is None:
            dsn, stop_db = embedded_agent_dsn(out_dir.parent / "agent_pg")
            stack.callback(stop_db)
        stores = stack.enter_context(open_stores(schema=f"leaklab_s6_{int(time.time())}"))
        stack.callback(stores.repo.drop_schema)
        for label, poll in (
            ("membership", stores.membership.poll_once),
            ("acl", stores.acl.poll_once),
        ):
            result = poll()
            if not result.ok:
                raise RunnerError(f"Setup B first {label} sync failed: {result.error}")
        stop = threading.Event()
        threads = start_pollers(stores, stop, 5.0, 60.0)

        def stop_pollers() -> None:
            stop.set()
            for t in threads:
                t.join(timeout=60)

        stack.callback(stop_pollers)
        service = build_service(stores)
        stack.callback(service.close)
        server = stack.enter_context(LocalServer(create_app(service)))
        started = time.time()
        while not stores.readiness.state().ready and time.time() - started < READY_CAP_S:
            time.sleep(2)
        ready = stores.readiness.state().ready
        log(f"Setup B ready: {ready} after {time.time() - started:.0f} s")
        endpoint = get_settings().search_endpoint.strip() or saved_endpoint()
        search = HttpSearchPort(endpoint)
        stack.callback(search.close)
        cache_of_tokens = TokenCache(
            lambda upn: get_persona_token(
                upn, [SEARCH_SCOPE], allow_device_code=False, use_cache=False
            ),
            time.time,
            max_age_s=TOKEN_MAX_AGE_S,
        )
        access = StateAccess(stores.reader, env.generation)
        factory = make_arm_factory(
            dsn=dsn,
            stack=stack,
            llm=llm,
            embedder=embedder,
            clock=time.time,
            spec=spec,
            search=search,
            b_url=server.url,
            tokens=cache_of_tokens.get,
            access=access,
            cache_threshold=queries.cache_threshold,
            memory_threshold=queries.memory_threshold,
            run_tag=stamp.lower(),
        )
        with Journal(path) as journal:
            driver = S6Driver(
                queries=queries,
                users=users,
                env_ops=TenantOps(env, spec),
                arm_factory=factory,
                journal=journal.append,
                clock=time.time,
                sleep=time.sleep,
                config=DriverConfig(n=n, settle_s=settle_s),
            )
            log(f"running S6 into {path.name}")
            driver.run(
                variants=variants,
                header={
                    "generation": env.generation,
                    "llm": llm.name,
                    "exploratory": exploratory,
                    "embedder": f"hash-{embedder.dims}",
                    "spec_sha256": spec_sha256(spec_path),
                    "tenant": tenant,
                    "index_cleanup": cleanup,
                    "setup_b_ready": ready,
                },
            )
    return path
