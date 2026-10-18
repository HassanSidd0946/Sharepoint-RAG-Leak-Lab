"""The S6 driver: runs the fixed query set against the agent arms and journals every step.

It is NOT the mutation runner (that one polls a read every few seconds and measures convergence);
S6 asks a handful of fixed questions in a fixed order. It reuses the runner's Journal, the
generator's apply_mutation (through EnvOps) and, at scoring time, the oracle.

Per trial (variant, arm, trial i = 1..n; follow-up i of the variant's query set):

  S6a cross_user_cache       owner asks the seed question; reader (no access) asks follow-up i
  S6b cache_after_revocation the user asks the seed; their grant is revoked (the S1-direct scenario's
                             mutation, confirmed); the driver waits `settle_s`; the user asks
                             follow-up i; the grant is restored and the retrieval waited for
  S6c shared_memory          owner asks the seed; reader asks a DIFFERENT question that the cache does
                             not answer but the memory does
  control positive_control   owner and reader may both read the source; the hit must be served

Before every trial the arm's cache, memory and thread registry are cleared and the driver waits until
the arm's own retrieval returns the source for the owner (a new tenant takes minutes to be indexed).
Scoring happens afterwards, from the journal (scoring.py), never in here.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from leaklab.agent.graph import Agent
from leaklab.agent.queries import QuerySet, Variant
from leaklab.agent.retrieval import Retriever
from leaklab.agent.stores import MemoryStore, SemanticCache
from leaklab.agent.threads import ThreadRegistry
from leaklab.agent.types import UserCtx
from leaklab.generator import LogEntry

ARMS: tuple[tuple[str, str, str], ...] = (
    ("agent_naive_a", "naive", "setup_a"),
    ("agent_naive_b", "naive", "setup_b"),
    ("agent_guarded_b", "guarded", "setup_b"),
)
NOT_APPLICABLE = {
    "setup0": "plain SharePoint has no search index, no cache and no agent: there is nothing to test"
}
VARIANT_ORDER = ("S6a", "S6b", "S6c", "control")


class EnvOps(Protocol):
    """What the driver needs from the tenant."""

    def revoke(self, scenario: str) -> LogEntry:
        """Apply the scenario's mutation (confirmed before returning, as apply_mutation does)."""
        ...

    def restore(self, scenario: str) -> LogEntry:
        """Undo it (the inverse mutation)."""
        ...


@dataclass
class ArmRuntime:
    name: str
    agent: Agent
    cache: SemanticCache
    memory: MemoryStore
    threads: ThreadRegistry
    retriever: Retriever


ArmFactory = Callable[[str, str, str, Callable[[dict[str, Any]], None]], ArmRuntime]


@dataclass
class DriverConfig:
    n: int = 3
    settle_s: float = 30.0  # after a confirmed revocation, before the stale-cache question
    baseline_wait_s: float = 420.0  # how long to wait for the owner's source to be retrievable
    restore_wait_s: float = 420.0
    poll_s: float = 10.0


@dataclass
class S6Driver:
    queries: QuerySet
    users: Mapping[str, UserCtx]
    env_ops: EnvOps
    arm_factory: ArmFactory
    journal: Callable[[dict[str, Any]], None]
    clock: Callable[[], float]
    sleep: Callable[[float], None]
    config: DriverConfig = field(default_factory=DriverConfig)
    arms: dict[str, ArmRuntime] = field(default_factory=dict)
    _ctx: dict[str, Any] = field(default_factory=dict)

    # -- run ---------------------------------------------------------------------------------------

    def run(
        self,
        arms: Sequence[tuple[str, str, str]] = ARMS,
        variants: Sequence[str] = VARIANT_ORDER,
        header: Mapping[str, Any] | None = None,
    ) -> None:
        for name, mode, retr in arms:
            self.arms[name] = self.arm_factory(name, mode, retr, self.agent_journal)
        self.journal(
            {
                "type": "s6_started",
                "t": self.clock(),
                "version": self.queries.version,
                "queries": self.queries.model_dump(),
                "arms": [{"name": n, "mode": m, "retrieval": r} for n, m, r in arms],
                "n": self.config.n,
                "settle_s": self.config.settle_s,
                "not_applicable": NOT_APPLICABLE,
                **(header or {}),
            }
        )
        for variant in variants:
            for name, _, _ in arms:
                for trial in range(1, self.config.n + 1):
                    self.run_trial(variant, name, trial)
        self.journal({"type": "run_finished", "t": self.clock()})

    def agent_journal(self, event: dict[str, Any]) -> None:
        """What the agent reports (lookups, guard decisions), tagged with the trial it belongs to."""
        self.journal({"type": "s6_agent", **self._ctx, **event})

    # -- one trial ---------------------------------------------------------------------------------

    def run_trial(self, variant: str, arm_name: str, trial: int) -> None:
        v = self.queries.variants[variant]
        arm = self.arms[arm_name]
        self._ctx = {"variant": variant, "arm": arm_name, "trial": trial}
        self.journal({"type": "s6_trial_started", "t": self.clock(), **self._ctx, "kind": v.kind})
        arm.cache.clear()
        arm.memory.clear()
        arm.threads.clear()
        asker = self.users[v.user or v.owner or ""]
        self._wait_retrievable(
            arm, asker, v.seed, v.source, self.config.baseline_wait_s, "baseline"
        )
        followup = v.followups[(trial - 1) % len(v.followups)]
        if v.kind == "cache_after_revocation":
            self._stale_cache_trial(v, arm, asker, followup)
        else:
            reader = self.users[v.reader or ""]
            self._step(arm, "seed", asker, v.seed)
            self._step(arm, "followup", reader, followup)
        if v.kind != "cache_after_revocation":  # that trial snapshots before its restore
            self._snapshot(arm)
        self.journal({"type": "s6_trial_done", "t": self.clock(), **self._ctx})

    def _stale_cache_trial(self, v: Variant, arm: ArmRuntime, user: UserCtx, followup: str) -> None:
        assert v.scenario is not None
        self._step(arm, "seed", user, v.seed)
        entry = self.env_ops.revoke(v.scenario)
        self.journal(
            {
                "type": "mutation",
                "scenario": v.scenario,
                "role": "revoke",
                **self._ctx,
                "entry": entry.model_dump(mode="json"),
            }
        )
        self.sleep(self.config.settle_s)
        self._step(arm, "followup", user, followup)
        self._snapshot(arm)  # exposure is read at the end of the attack, before the restore
        restored = self.env_ops.restore(v.scenario)
        self.journal(
            {
                "type": "mutation",
                "scenario": v.scenario,
                "role": "restore",
                **self._ctx,
                "entry": restored.model_dump(mode="json"),
            }
        )
        self.sleep(self.config.settle_s)
        self._wait_retrievable(arm, user, v.seed, v.source, self.config.restore_wait_s, "restore")

    # -- helpers -----------------------------------------------------------------------------------

    def _step(self, arm: ArmRuntime, step: str, user: UserCtx, question: str) -> None:
        t0 = self.clock()
        thread = f"s6-{self._ctx['variant']}-{arm.name}-{self._ctx['trial']}-{user.persona}"
        record: dict[str, Any] = {
            "type": "s6_step",
            **self._ctx,
            "step": step,
            "persona": user.persona,
            "question": question,
            "t0": t0,
        }
        try:
            delivery = arm.agent.ask(user, question, thread_id=thread)
        except Exception as exc:  # a step that fails is journalled, scored INVALID, never hidden
            record.update({"t1": self.clock(), "error": f"{type(exc).__name__}: {str(exc)[:200]}"})
        else:
            record.update(
                {
                    "t1": self.clock(),
                    "answer": delivery.answer,
                    "via": delivery.via,
                    "cache": delivery.cache,
                    "memory": delivery.memory,
                    "passages": delivery.passages,
                    "guard": delivery.guard,
                }
            )
        self.journal(record)

    def _snapshot(self, arm: ArmRuntime) -> None:
        """The text now sitting in the cache and the memory: the exposure surface (see scoring.py)."""
        texts = [e.answer for e in arm.cache.entries()] + [i.text for i in arm.memory.items()]
        self.journal({"type": "s6_snapshot", "t": self.clock(), **self._ctx, "texts": texts})

    def _wait_retrievable(
        self, arm: ArmRuntime, user: UserCtx, question: str, source: str, cap_s: float, why: str
    ) -> None:
        """Wait until the arm's own retrieval returns the SOURCE document (its title appears in a
        returned passage) for `user`: indexing and state take minutes after a new tenant."""
        started = self.clock()
        ok = False
        error = None
        while True:
            try:
                ok = any(source in p.content for p in arm.retriever.retrieve(user, question, 3))
                error = None
            except Exception as exc:
                ok, error = False, type(exc).__name__
            if ok or self.clock() - started >= cap_s:
                break
            self.sleep(self.config.poll_s)
        self.journal(
            {
                "type": "s6_wait",
                "t": self.clock(),
                **self._ctx,
                "why": why,
                "ok": ok,
                "waited_s": round(self.clock() - started, 3),
                "error": error,
            }
        )
