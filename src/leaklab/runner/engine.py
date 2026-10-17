"""The scenario engine: baseline -> mutate -> poll, for several scenarios in one polling loop.

Per scenario the engine walks three phases:
  baseline  read the scenario's pairs every cycle until every pair has `baseline_k` conforming
            reads in a row (errors are skipped), judged against the oracle's intent (cap
            `baseline_cap_s`; on the cap the baseline is flagged
            BASELINE_UNSTABLE and the run CONTINUES, because the instability is itself a result).
  mutate    apply the scenario's mutations through generator.apply_mutation; its `confirmed_at`
            is the moment intent changes.
  polling   read the pairs every cycle until every flipped pair has `n_consecutive` conforming
            reads in a row, or `cap_s` has passed. A scenario whose intent does not flip (S4) is
            watched for `steady_window_s` instead.
Scenarios whose pairs do not overlap share one loop (scenarios.group_scenarios); every cycle reads
each needed (system, persona, canary) once.

Robustness:
  - a read that fails or raises is an Observation with served=None (an error), never a denial and
    never a reason to stop the run;
  - every event goes to the append-only journal as it happens; `resume=` replays a journal and
    continues where it stopped. A crash between "mutation_started" and its outcome cannot be
    resolved by the engine (the tenant may or may not have changed): it raises, and a human checks;
  - time comes from env.clock / env.sleep, the same clock the mutation log uses.

Conformance of a read: served == (canary in oracle intent at the read's time). Reads with
served=None are ignored (they neither advance nor reset a streak), exactly as in oracle.scoring.
"""

import logging
from collections import Counter
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from leaklab.generator import Env, LogEntry, apply_mutation
from leaklab.oracle import IntentTimeline, Mutation, MutationType
from leaklab.runner.journal import Event, Journal
from leaklab.runner.model import Observation, Pair, RunConfig, RunnerError
from leaklab.runner.scenarios import (
    ScenarioPlan,
    derive_plan,
    group_scenarios,
    plan_from_dict,
    plan_to_dict,
)
from leaklab.runner.system import SystemUnderTest
from leaklab.spec import TenantSpec

log = logging.getLogger(__name__)

BASELINE_OK = "BASELINE_OK"
BASELINE_UNSTABLE = "BASELINE_UNSTABLE"


@dataclass
class _Scenario:
    plan: ScenarioPlan
    phase: str = "baseline"  # baseline | polling | done
    baseline_started: float = 0.0
    baseline_status: str | None = None
    mutation_started: bool = False
    mutation_outcome: bool = False  # a "mutation" or "mutation_failed" event was seen
    t_change: float | None = None
    post_cycles: int = 0
    done_reason: str | None = None
    entries: list[LogEntry] = field(default_factory=list)


def restore_env(env: Env, events: Sequence[Event]) -> list[Mutation]:
    """Bring a fresh Env in line with a journal: log entries and where special files now are.

    Returns the oracle mutations (with confirmed_at) of every applied mutation, in order.
    """
    applied: list[Mutation] = []
    for event in events:
        if event["type"] != "mutation":
            continue
        entry = LogEntry.model_validate(event["entry"])
        env.log.add(entry)
        if entry.mutation.type is MutationType.FILE_MOVED and entry.status != "failed":
            env.special_folder[str(entry.mutation.params["file"])] = str(
                entry.mutation.params["to_folder"]
            )
        if entry.status != "failed":
            applied.append(entry.mutation.model_copy(update={"confirmed_at": entry.confirmed_at}))
    return applied


class Engine:
    def __init__(
        self,
        *,
        env: Env,
        spec: TenantSpec,
        systems: Sequence[SystemUnderTest],
        config: RunConfig,
        journal: Journal,
        applied: Sequence[Mutation] = (),
    ) -> None:
        self.env = env
        self.spec = spec
        self.systems = list(systems)
        self.config = config
        self.journal = journal
        self.applied: list[Mutation] = list(applied)
        self.clock = env.clock
        self.sleep = env.sleep
        self.streaks: dict[tuple[str, Pair], int] = {}
        self.errors: Counter[str] = Counter()
        self._timeline_cache: tuple[int, IntentTimeline] | None = None

    # -- public --------------------------------------------------------------------------------

    def run(
        self,
        scenario_ids: Sequence[str],
        replay: Sequence[Event] = (),
    ) -> None:
        """Run the scenarios (grouped); with `replay`, continue the run that journal describes."""
        started = _split_group_events(replay)
        taken: set[str] = set()
        for index in sorted(started):  # groups the journal already knows keep their members
            events = started[index]
            ids = [
                str(p["id"])
                for p in next(e for e in events if e["type"] == "group_started")["scenarios"]
            ]
            taken.update(ids)
            if not _group_finished(events, ids):
                self._run_group(index, ids, events)
        remaining = [sid for sid in scenario_ids if sid not in taken]
        grouped = group_scenarios([derive_plan(self.spec, sid, self.applied) for sid in remaining])
        first = max(started, default=-1) + 1
        for offset, group in enumerate(grouped):
            self._run_group(first + offset, [p.id for p in group], [])

    # -- one group --------------------------------------------------------------------------------

    def _run_group(self, index: int, ids: Sequence[str], events: Sequence[Event]) -> None:
        resumed = bool(events)
        if resumed:
            started = next(e for e in events if e["type"] == "group_started")
            plans = [plan_from_dict(self.spec, p) for p in started["scenarios"]]
            self.journal.append({"type": "group_resumed", "group": index, "t": self.clock()})
        else:
            plans = [derive_plan(self.spec, sid, self.applied) for sid in ids]
        states = {p.id: _Scenario(plan=p) for p in plans}
        canaries = sorted({c for p in plans for c in p.canaries()})
        personas = sorted({x for p in plans for x in p.personas()})
        for system in self.systems:
            system.prepare(personas, canaries)  # signs in; raises (before any mutation) if it can't

        now = self.clock()
        for s in states.values():
            s.baseline_started = now
        self.streaks.clear()  # a group's baseline stands on the group's own reads
        if resumed:
            cycle = self._replay(events, states)
        else:
            self.journal.append(
                {
                    "type": "group_started",
                    "group": index,
                    "t": now,
                    "systems": [s.name for s in self.systems],
                    "scenarios": [plan_to_dict(p) for p in plans],
                }
            )
            cycle = 0
        for s in states.values():  # a crash after baseline_done but before the mutation started
            if s.phase == "baseline" and s.baseline_status is not None and not s.mutation_started:
                self._mutate(s)
        self._loop(states, cycle)

    def _loop(self, states: dict[str, _Scenario], cycle: int) -> None:
        while True:
            active = [s for s in states.values() if s.phase != "done"]
            if not active:
                return
            started = self.clock()
            pairs = sorted({pair for s in active for pair in s.plan.pairs})
            batch = self._read_cycle(pairs, cycle)
            self._feed(batch)
            for s in active:
                self._advance(s)
            cycle += 1
            self.sleep(max(0.0, started + self.config.interval_s - self.clock()))

    # -- reading ----------------------------------------------------------------------------------

    def _safe_read(self, system: SystemUnderTest, pair: Pair) -> Observation:
        persona, canary = pair
        try:
            obs = system.read(persona, canary)
        except Exception as exc:  # a misbehaving system must not stop the run
            return Observation(
                system=system.name,
                persona=persona,
                canary=canary,
                ts=self.clock(),
                served=None,
                error=f"exception:{type(exc).__name__}",
            )
        return obs

    def _read_cycle(self, pairs: Sequence[Pair], cycle: int) -> list[Observation]:
        jobs = [(system, pair) for pair in pairs for system in self.systems]
        batch: list[Observation] = []

        def record(obs: Observation) -> None:  # journal each read as soon as it is known
            batch.append(obs)
            self.journal.append({"type": "obs", "cycle": cycle, **obs.model_dump()})

        if self.config.workers > 1 and len(jobs) > 1:
            with ThreadPoolExecutor(max_workers=self.config.workers) as pool:
                for obs in pool.map(lambda job: self._safe_read(*job), jobs):
                    record(obs)
        else:
            for system, pair in jobs:
                record(self._safe_read(system, pair))
        return batch

    # -- judging reads --------------------------------------------------------------------------

    def _timeline(self) -> IntentTimeline:
        key = len(self.applied)
        if self._timeline_cache is None or self._timeline_cache[0] != key:
            self._timeline_cache = (key, IntentTimeline(self.spec, self.applied))
        return self._timeline_cache[1]

    def _conforms(self, obs: Observation) -> bool | None:
        """True/False if the read matches intent or not; None for an error (unknown)."""
        if obs.served is None:
            return None
        allowed = obs.canary in self._timeline().expected_at(obs.ts).get(obs.persona, ())
        return obs.served == allowed

    def _feed(self, batch: Sequence[Observation]) -> None:
        for obs in batch:
            key = (obs.system, (obs.persona, obs.canary))
            verdict = self._conforms(obs)
            if verdict is None:
                self.errors[obs.system] += 1
            elif verdict:
                self.streaks[key] = self.streaks.get(key, 0) + 1
            else:
                self.streaks[key] = 0

    def _lowest_streak(self, pairs: Sequence[Pair] | frozenset[Pair]) -> int:
        """The shortest run of conforming reads among the pairs (errors are skipped)."""
        names = [system.name for system in self.systems]
        return min(
            (self.streaks.get((name, pair), 0) for name in names for pair in pairs), default=0
        )

    # -- phases ----------------------------------------------------------------------------------

    def _advance(self, s: _Scenario) -> None:
        now = self.clock()
        if s.phase == "baseline":
            lowest = self._lowest_streak(s.plan.pairs)
            stable = lowest >= self.config.baseline_k
            if stable or now - s.baseline_started >= self.config.baseline_cap_s:
                s.baseline_status = BASELINE_OK if stable else BASELINE_UNSTABLE
                self.journal.append(
                    {
                        "type": "baseline_done",
                        "scenario": s.plan.id,
                        "status": s.baseline_status,
                        "t": now,
                        "min_streak": lowest,
                    }
                )
                self._mutate(s)
        elif s.phase == "polling":
            self._check_done(s, now)

    def _finish(self, s: _Scenario, reason: str) -> None:
        s.phase, s.done_reason = "done", reason
        self.journal.append(
            {
                "type": "scenario_done",
                "scenario": s.plan.id,
                "reason": reason,
                "t": self.clock(),
                "t_change": s.t_change,
            }
        )

    def _mutate(self, s: _Scenario) -> None:
        s.mutation_started = True
        self.journal.append({"type": "mutation_started", "scenario": s.plan.id, "t": self.clock()})
        for mutation in s.plan.mutations:
            try:
                entry = apply_mutation(self.env, mutation)
            except Exception as exc:  # GeneratorError, GraphError, SPRestError, network errors
                self.journal.append(
                    {
                        "type": "mutation_failed",
                        "scenario": s.plan.id,
                        "error": f"{type(exc).__name__}: {exc}",
                        "t": self.clock(),
                    }
                )
                s.mutation_outcome = True
                self._finish(s, "mutation_failed")
                return
            self.journal.append(
                {"type": "mutation", "scenario": s.plan.id, "entry": entry.model_dump(mode="json")}
            )
            s.entries.append(entry)
            if entry.confirmed_at is None:
                s.mutation_outcome = True
                self._finish(s, "mutation_unconfirmed")
                return
            self.applied.append(
                entry.mutation.model_copy(update={"confirmed_at": entry.confirmed_at})
            )
        s.mutation_outcome = True
        self._start_polling(s, max(e.confirmed_at or 0.0 for e in s.entries))

    def _start_polling(self, s: _Scenario, t_change: float) -> None:
        s.phase, s.t_change, s.post_cycles = "polling", t_change, 0
        # Reads before the change were judged against the old intent: they must not count towards
        # "N conforming reads in a row" after it.
        for system in self.systems:
            for pair in s.plan.pairs:
                self.streaks[(system.name, pair)] = 0

    def _check_done(self, s: _Scenario, now: float) -> None:
        assert s.t_change is not None
        s.post_cycles += 1
        n = self.config.n_consecutive
        if s.plan.flipped:
            done = all(
                self.streaks.get((system.name, pair), 0) >= n
                for system in self.systems
                for pair in s.plan.flipped
            )
            reason = "converged"
        else:
            done = now - s.t_change >= self.config.steady_window_s and s.post_cycles >= n
            reason = "steady_window_elapsed"
        if done:
            self._finish(s, reason)
        elif now - s.t_change >= self.config.cap_s:
            self._finish(s, "cap")

    # -- resume ---------------------------------------------------------------------------------

    def _replay(self, events: Sequence[Event], states: dict[str, _Scenario]) -> int:
        """Rebuild the in-memory state from the journal's events for this group; next cycle no."""
        pending: list[Observation] = []
        pending_cycle = -1
        last_cycle = -1

        def flush() -> None:
            if pending:
                self._feed(pending)
                pending.clear()

        for e in events:
            kind = e["type"]
            if kind == "obs":
                if pending and e["cycle"] != pending_cycle:
                    flush()
                pending_cycle = last_cycle = int(e["cycle"])
                pending.append(
                    Observation.model_validate(
                        {k: v for k, v in e.items() if k not in ("type", "cycle")}
                    )
                )
                continue
            flush()
            s = states.get(str(e.get("scenario", "")))
            if s is None:
                continue
            elif kind == "baseline_done":
                s.baseline_status = str(e["status"])
            elif kind == "mutation_started":
                s.mutation_started = True
            elif kind == "mutation":
                entry = LogEntry.model_validate(e["entry"])
                s.entries.append(entry)
                if entry.confirmed_at is None:
                    s.mutation_outcome = True
            elif kind == "mutation_failed":
                s.mutation_outcome = True
                s.phase = "done"
                s.done_reason = "mutation_failed"
            elif kind == "scenario_done":
                s.phase, s.done_reason = "done", str(e["reason"])
                s.t_change = e.get("t_change")
        flush()
        group_t = next((float(e["t"]) for e in events if e["type"] == "group_started"), None)
        last_t = max((float(e["ts"]) for e in events if e["type"] == "obs"), default=None)
        if group_t is not None and last_t is not None:
            # time already spent in the baseline before the crash still counts towards its cap
            for s in states.values():
                s.baseline_started = self.clock() - max(0.0, last_t - group_t)
        for s in states.values():
            if s.phase == "done":
                continue
            if s.mutation_started and not s.mutation_outcome:
                if len(s.entries) < len(s.plan.mutations):
                    raise RunnerError(
                        f"the run stopped while applying the mutation of {s.plan.id!r}: the tenant "
                        "may or may not have changed. Check it (generator verify) and start a new "
                        "run; the engine will not guess."
                    )
            if s.entries and len(s.entries) == len(s.plan.mutations):
                confirmed = [e.confirmed_at for e in s.entries if e.confirmed_at is not None]
                if len(confirmed) == len(s.entries):
                    s.mutation_outcome = True
                    s.phase, s.t_change, s.post_cycles = "polling", max(confirmed), 0
                    for system in self.systems:
                        for pair in s.plan.pairs:
                            self.streaks[(system.name, pair)] = 0
        return last_cycle + 1


def _split_group_events(events: Sequence[Event]) -> dict[int, list[Event]]:
    """Group a journal's events by the group they belong to (events follow their group_started)."""
    out: dict[int, list[Event]] = {}
    current: int | None = None
    for e in events:
        if e["type"] == "group_started":
            current = int(e["group"])
            out[current] = []
        if current is not None and e["type"] not in ("run_finished",):
            out[current].append(e)
    return out


def _group_finished(events: Sequence[Event], ids: Sequence[str]) -> bool:
    done = {str(e["scenario"]) for e in events if e["type"] == "scenario_done"}
    return set(ids) <= done
