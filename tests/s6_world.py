"""A fake tenant for the S6 driver: real spec documents, access from the ORACLE, a clock.

Who may read what at time t is `IntentTimeline(spec, applied).expected_at(t)`: exactly what the
scoring uses, so the fake systems and the scorer cannot disagree about the facts. Setup A is
modelled as an index whose ACL is FROZEN at the start (it ignores revocations until "reindexed"),
Setup B and the guard follow the current intent.
"""

from collections.abc import Callable, Sequence
from typing import Any

from leaklab.agent.embedding import HashEmbedder, tokens
from leaklab.agent.graph import Agent, AgentConfig
from leaklab.agent.guard import AccessResult
from leaklab.agent.llm import StubLLM
from leaklab.agent.queries import load_queries
from leaklab.agent.s6.driver import ArmFactory, ArmRuntime, DriverConfig, S6Driver
from leaklab.agent.stores import MemoryCache, MemoryMemory
from leaklab.agent.threads import MemoryThreads
from leaklab.agent.types import Passage, UserCtx
from leaklab.generator import LogEntry
from leaklab.generator.plan import build_plan
from leaklab.oracle import IntentTimeline, Mutation, MutationType
from leaklab.spec import TenantSpec, generate_canaries
from runner_fakes import SPEC as BASE_SPEC

GENERATION = "GS6TEST"
SPEC = BASE_SPEC.with_generation(GENERATION)


class Clock:
    def __init__(self) -> None:
        self.now = 10_000.0

    def __call__(self) -> float:
        self.now += 1.0
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class World:
    """Documents (from the spec's plan), the applied mutations and the current intent."""

    def __init__(self, spec: TenantSpec = SPEC) -> None:
        self.spec = spec
        self.clock = Clock()
        self.applied: list[Mutation] = []
        plan = build_plan(spec)
        self.docs: dict[str, tuple[str, str, str]] = {}  # item id -> (title, text, canary)
        for folder in plan.folders:
            for d in folder.docs:
                self.docs[f"item-{d.canary}"] = (folder.path, d.text, d.canary)
        self.canary_of = {i: v[2] for i, v in self.docs.items()}
        self.users = {
            u.id: UserCtx(u.id, f"oid-{u.id}", f"{u.id}@x") for u in spec.users if u.persona
        }
        self.persona_of = {u.oid: p for p, u in self.users.items()}
        self.frozen_at: float | None = None  # Setup A's index: the intent it was last built from
        self.seq = 0

    def allowed(self, oid: str, item_id: str, *, frozen: bool = False) -> bool:
        t = self.frozen_at if frozen and self.frozen_at is not None else self.clock.now
        timeline = IntentTimeline(self.spec, self.applied)
        persona = self.persona_of.get(oid)
        canary = self.canary_of.get(item_id)
        return (
            persona is not None
            and canary is not None
            and canary in timeline.expected_at(t).get(persona, set())
        )

    def entry(self, m: Mutation, *, confirmed: bool = True) -> LogEntry:
        self.seq += 1
        now = self.clock()
        if confirmed:
            self.applied.append(m.model_copy(update={"confirmed_at": now}))
        return LogEntry(
            seq=self.seq,
            mutation=m,
            requested_at=now,
            executed_at=now,
            confirmed_at=now if confirmed else None,
            status="confirmed" if confirmed else "unconfirmed",
        )


class WorldEnvOps:
    """revoke / restore of the S1-direct scenario's grant, on the fake world."""

    def __init__(self, world: World, *, confirm: bool = True) -> None:
        self.world = world
        self.confirm = confirm
        self.params = {
            "target": "S1-Direct",
            "principal_kind": "user",
            "principal_ref": "p-plateng",
        }

    def revoke(self, scenario: str) -> LogEntry:
        assert scenario == "S1-direct"
        return self.world.entry(
            Mutation(type=MutationType.GRANT_REMOVED, params=self.params), confirmed=self.confirm
        )

    def restore(self, scenario: str) -> LogEntry:
        return self.world.entry(
            Mutation(type=MutationType.GRANT_ADDED, params={**self.params, "role": "read"})
        )


class WorldRetriever:
    """Keyword retrieval over the world's documents, trimmed to what the user may read now
    (Setup B) or to what they could read when `frozen_at` was taken (Setup A's frozen ACL)."""

    def __init__(self, world: World, name: str, *, frozen: bool) -> None:
        self.world, self.name, self.frozen = world, name, frozen

    def retrieve(self, user: UserCtx, question: str, k: int) -> list[Passage]:
        wanted = set(tokens(question))
        scored = []
        for item, (_folder, text, _canary) in self.world.docs.items():
            if not self.world.allowed(user.oid, item, frozen=self.frozen):
                continue
            score = len(
                wanted
                & set(tokens(text.splitlines()[0] + " " + text.split("Topic:")[1].splitlines()[0]))
            )
            if score:
                scored.append((-score, text.splitlines()[0], item, text))
        scored.sort(key=lambda t: t[:2])
        return [Passage(item, title, text) for _, title, item, text in scored[:k]]


class WorldAccess:
    generation = GENERATION

    def __init__(self, world: World) -> None:
        self.world = world

    def check(self, user_oid: str, item_ids: Sequence[str]) -> list[AccessResult]:
        return [
            AccessResult(
                i, ok := self.world.allowed(user_oid, i), "allowed" if ok else "no_matching_grant"
            )
            for i in item_ids
        ]


def arm_factory(world: World, **config: Any) -> ArmFactory:
    def make(
        name: str, mode: str, retrieval: str, journal: Callable[[dict[str, Any]], None]
    ) -> ArmRuntime:
        cache, memory, threads = MemoryCache(), MemoryMemory(), MemoryThreads()
        retriever = WorldRetriever(world, retrieval, frozen=retrieval == "setup_a")
        agent = Agent(
            config=AgentConfig(mode=mode, generation=GENERATION, **config),  # type: ignore[arg-type]
            retriever=retriever,
            llm=StubLLM(),
            embedder=HashEmbedder(),
            cache=cache,
            memory=memory,
            threads=threads,
            clock=world.clock,
            access=WorldAccess(world) if mode == "guarded" else None,
            journal=journal,
        )
        return ArmRuntime(name, agent, cache, memory, threads, retriever)

    return make


def run_s6(
    *,
    n: int = 3,
    settle_s: float = 30.0,
    confirm: bool = True,
    arms: Sequence[tuple[str, str, str]] | None = None,
    variants: Sequence[str] | None = None,
    **config: Any,
) -> tuple[list[dict[str, Any]], World]:
    world = World()
    world.frozen_at = world.clock.now  # Setup A's index is built now
    events: list[dict[str, Any]] = []
    driver = S6Driver(
        queries=load_queries(),
        users=world.users,
        env_ops=WorldEnvOps(world, confirm=confirm),
        arm_factory=arm_factory(world, **config),
        journal=events.append,
        clock=world.clock,
        sleep=world.clock.sleep,
        config=DriverConfig(
            n=n, settle_s=settle_s, baseline_wait_s=30.0, restore_wait_s=30.0, poll_s=5.0
        ),
    )
    kwargs: dict[str, Any] = {
        "header": {"generation": GENERATION, "llm": "stub", "embedder": "hash-256"}
    }
    if arms is not None:
        kwargs["arms"] = arms
    if variants is not None:
        kwargs["variants"] = variants
    driver.run(**kwargs)
    return events, world


__all__ = ["World", "run_s6", "SPEC", "GENERATION", "generate_canaries"]
