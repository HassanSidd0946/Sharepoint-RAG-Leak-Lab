"""Turn a scenario from tenant_spec.yaml into something the engine can poll.

A ScenarioPlan says WHICH (persona, canary) pairs to read and what the oracle expects of them:
  flipped   pairs whose intent changes because of the mutation (revoking: allowed -> denied;
            granting: denied -> allowed). These decide when the scenario has converged.
  targeted  one canary of every object the mutation touches, read by every persona, whose intent
            does NOT change (they catch a leak or a lockout caused by the change itself).
  control   one pair the mutation must not affect (the persona stays allowed). A control that
            starts failing means the whole system, not the change, is misbehaving.
Everything is derived from the spec and the oracle: nothing is read from the tenant.

Scenarios that touch disjoint pairs can share one polling loop; group_scenarios() does that split.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from leaklab.oracle import Mutation, MutationType, expected_access
from leaklab.runner.model import Pair, RunnerError
from leaklab.spec import Scenario, TenantSpec, generate_canaries, special_key

_TARGETED_TYPES = (
    MutationType.GRANT_ADDED,
    MutationType.GRANT_REMOVED,
    MutationType.INHERITANCE_BROKEN,
    MutationType.INHERITANCE_RESTORED,
)


@dataclass(frozen=True)
class ScenarioPlan:
    id: str
    mutations: tuple[Mutation, ...]  # as requested: confirmed_at is None
    revoking: frozenset[Pair]
    granting: frozenset[Pair]
    targeted: frozenset[Pair]
    control: Pair | None

    @property
    def flipped(self) -> frozenset[Pair]:
        return self.revoking | self.granting

    @property
    def watched(self) -> frozenset[Pair]:
        """Pairs whose intent the scenario itself touches (changed or merely targeted)."""
        return self.flipped | self.targeted

    @property
    def pairs(self) -> frozenset[Pair]:
        return self.watched | ({self.control} if self.control else frozenset())

    def personas(self) -> set[str]:
        return {p for p, _ in self.pairs}

    def canaries(self) -> set[str]:
        return {c for _, c in self.pairs}


def scenario_mutations(scenario: Scenario) -> tuple[Mutation, ...]:
    """The scenario's mutations as oracle Mutations (validated there)."""
    if not scenario.mutations:
        raise RunnerError(
            f"scenario {scenario.id!r} has no mutations: it is not a tenant change the runner "
            "can apply (S6 is a cache test, for a later stage)"
        )
    try:
        return tuple(
            Mutation(type=MutationType(m.type), params=m.params) for m in scenario.mutations
        )
    except ValueError as exc:
        raise RunnerError(f"scenario {scenario.id!r}: {exc}") from exc


def _target_keys(spec: TenantSpec, mutation: Mutation) -> list[str]:
    """Keys of generate_canaries() for the objects a mutation directly touches."""
    p = mutation.params
    if mutation.type is MutationType.FILE_MOVED:
        return [special_key(str(p["file"]))]
    if mutation.type in _TARGETED_TYPES and p["target"] != "root":
        return [str(p["target"])]
    return []


def derive_plan(spec: TenantSpec, scenario_id: str, prior: Sequence[Mutation] = ()) -> ScenarioPlan:
    """The plan for one scenario, given the mutations already applied (`prior`)."""
    scenario = next((s for s in spec.scenarios if s.id == scenario_id), None)
    if scenario is None:
        known = sorted(s.id for s in spec.scenarios)
        raise RunnerError(f"unknown scenario {scenario_id!r}; the spec has {known}")
    mutations = scenario_mutations(scenario)
    canaries = generate_canaries(spec)
    order = list(canaries)  # spec order: folders, then special files

    start = list(prior)
    base = max((m.confirmed_at or 0.0 for m in start), default=0.0)
    requested = [
        m.model_copy(update={"confirmed_at": base + i + 1}) for i, m in enumerate(mutations)
    ]
    before = expected_access(spec, start, float("inf"), canaries=canaries)
    after = expected_access(spec, [*start, *requested], float("inf"), canaries=canaries)

    revoking: set[Pair] = set()
    granting: set[Pair] = set()
    for persona in before:
        for canary in before[persona] - after[persona]:
            revoking.add((persona, canary))
        for canary in after[persona] - before[persona]:
            granting.add((persona, canary))
    flipped = revoking | granting

    keys: list[str] = []
    for m in mutations:
        keys += [k for k in _target_keys(spec, m) if k not in keys]
    targeted: set[Pair] = set()
    for key in keys:
        if canaries.get(key):
            targeted |= {(persona, canaries[key][0]) for persona in before}
    targeted -= flipped

    control = _pick_control(spec, scenario, before, after, order, canaries, flipped, targeted)
    return ScenarioPlan(
        id=scenario_id,
        mutations=mutations,
        revoking=frozenset(revoking),
        granting=frozenset(granting),
        targeted=frozenset(targeted),
        control=control,
    )


def _pick_control(
    spec: TenantSpec,
    scenario: Scenario,
    before: dict[str, set[str]],
    after: dict[str, set[str]],
    order: list[str],
    canaries: dict[str, list[str]],
    flipped: set[Pair],
    targeted: set[Pair],
) -> Pair | None:
    """A (persona, canary) the mutation must leave allowed: the scenario's `control` folder or,
    failing that, the first untouched canary the main persona can read."""
    persona_counts: dict[str, int] = {}
    for persona, _ in flipped:
        persona_counts[persona] = persona_counts.get(persona, 0) + 1
    main = sorted(persona_counts, key=lambda p: (-persona_counts[p], p))
    personas = main + [p for p in sorted(before) if p not in main]
    touched = {c for _, c in flipped | targeted}

    if scenario.control is not None:
        if not canaries.get(scenario.control):
            raise RunnerError(
                f"scenario {scenario.id!r}: control {scenario.control!r} has no canary"
            )
        canary = canaries[scenario.control][0]
        for persona in personas:
            if canary in before[persona] and canary in after[persona]:
                return (persona, canary)
        raise RunnerError(
            f"scenario {scenario.id!r}: nobody stays allowed on the control {scenario.control!r}"
        )
    for persona in personas:
        for key in order:
            for canary in canaries[key][:1]:
                if canary in before[persona] and canary in after[persona] and canary not in touched:
                    return (persona, canary)
    return None


def group_scenarios(plans: Sequence[ScenarioPlan]) -> list[list[ScenarioPlan]]:
    """Split scenarios into groups that can share one polling loop without interfering.

    Two scenarios interfere when one changes or targets a pair the other reads (its pairs or its
    control). Disjoint ones run together; the rest wait for a later group. Order is kept.
    """
    groups: list[list[ScenarioPlan]] = []
    for plan in plans:
        for group in groups:
            if all(
                not (plan.watched & other.pairs) and not (other.watched & plan.pairs)
                for other in group
            ):
                group.append(plan)
                break
        else:
            groups.append([plan])
    return groups


def plan_to_dict(plan: ScenarioPlan) -> dict[str, object]:
    """The plan as plain JSON (stored in the journal so `report` needs no re-derivation)."""
    return {
        "id": plan.id,
        "revoking": sorted(map(list, plan.revoking)),
        "granting": sorted(map(list, plan.granting)),
        "targeted": sorted(map(list, plan.targeted)),
        "control": list(plan.control) if plan.control else None,
    }


def plan_from_dict(spec: TenantSpec, data: dict[str, object]) -> ScenarioPlan:
    scenario = next((s for s in spec.scenarios if s.id == data["id"]), None)
    if scenario is None:
        raise RunnerError(f"the journal names scenario {data['id']!r}, which the spec lacks")

    def pairs(key: str) -> frozenset[Pair]:
        raw = data[key]
        assert isinstance(raw, list)
        return frozenset((str(a), str(b)) for a, b in raw)

    control = data["control"]
    return ScenarioPlan(
        id=scenario.id,
        mutations=scenario_mutations(scenario),
        revoking=pairs("revoking"),
        granting=pairs("granting"),
        targeted=pairs("targeted"),
        control=(str(control[0]), str(control[1])) if isinstance(control, list) else None,
    )
