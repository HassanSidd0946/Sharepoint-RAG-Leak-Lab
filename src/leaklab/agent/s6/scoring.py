"""Scoring S6 trials from a journal: a pure function of the events, the spec and the oracle.

The S6 journal (same Journal class and one-JSON-event-per-line format as the mutation runner) holds,
per trial, the steps (who asked what, what was delivered, how the cache and memory behaved) and the
tenant mutations (`mutation` events, same shape as the runner's, with confirmed_at). Scoring needs
nothing else:

  LEAK       a canary was DELIVERED to a persona whose expected access at that moment lacks it. The
             moment is the end of the step; "expected access" is the existing oracle
             (oracle.IntentTimeline over the spec and the confirmed mutations), per canary.
  OK         the situation the variant was designed for happened, and nothing was leaked.
  INVALID    the designed situation did not happen (no cache hit / no memory hit for the critical
             question, a step failed, the revocation was not confirmed, or nothing that could leak
             was stored: the seed answer holds no canary the asker may not, or no longer, read): the
             result says nothing about the system and is NEVER counted as OK. Applied to the guarded arm too: "no leak" is only
             evidence when the guard actually faced a cache or memory candidate.
  SERVED / OVERBLOCK (positive control only)  the cache hit was delivered / it was blocked although
             the user may read every source.

EXPOSURE SURFACE is different from a leak: canaries that sit in the cache or the memory and whose
source some persona may not read (computed from the store snapshots the driver journals). They are
what a leak could draw on; they are reported separately and never counted as leaks.
"""

import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from leaklab.generator import LogEntry
from leaklab.oracle import IntentTimeline, Mutation
from leaklab.spec import TenantSpec

CANARY_RE = re.compile(r"CANARY-G[0-9A-Z]{6}-(?:[A-Z0-9]+-\d{2}|SPC)-[0-9a-f]{6}")

LEAK, OK, INVALID = "LEAK", "OK", "INVALID"
SERVED, OVERBLOCK = "SERVED", "OVERBLOCK"
CACHE_KINDS = {"cross_user_cache", "cache_after_revocation", "positive_control"}


def canaries_in(text: str) -> list[str]:
    return sorted(set(CANARY_RE.findall(text or "")))


@dataclass
class Leak:
    persona: str
    canary: str
    t: float
    step: str


@dataclass
class TrialResult:
    variant: str
    arm: str
    trial: int
    kind: str
    verdict: str
    reason: str
    persona: str  # who asked the critical question
    question: str
    similarity: float | None  # cache similarity of the critical question
    cache_status: str  # hit | denied_by_guard | below_threshold | n/a
    delivered: list[str]  # canaries delivered at the critical step
    leaks: list[Leak] = field(default_factory=list)
    guard_denials: int = 0
    exposure_stored: int = 0  # canaries in the cache and memory at the end
    exposure_unsafe: int = 0  # of those, canaries some persona may not read
    exposure_pairs: int = 0  # (canary, persona) pairs without access


def timeline_of(spec: TenantSpec, events: Sequence[dict[str, Any]]) -> IntentTimeline:
    """The oracle's intent over every mutation the journal recorded (failed ones left out)."""
    applied: list[Mutation] = []
    for e in events:
        if e["type"] != "mutation":
            continue
        entry = LogEntry.model_validate(e["entry"])
        if entry.status != "failed":
            applied.append(entry.mutation.model_copy(update={"confirmed_at": entry.confirmed_at}))
    return IntentTimeline(spec, applied)


def _steps(
    events: Sequence[dict[str, Any]], variant: str, arm: str, trial: int
) -> list[dict[str, Any]]:
    return [
        e
        for e in events
        if e["type"] == "s6_step" and (e["variant"], e["arm"], e["trial"]) == (variant, arm, trial)
    ]


def _leaks(timeline: IntentTimeline, steps: Sequence[dict[str, Any]]) -> list[Leak]:
    found: list[Leak] = []
    for s in steps:
        if s.get("error") or not s.get("answer"):
            continue
        allowed = timeline.expected_at(float(s["t1"])).get(str(s["persona"]), set())
        for canary in canaries_in(str(s["answer"])):
            if canary not in allowed:
                found.append(Leak(str(s["persona"]), canary, float(s["t1"]), str(s["step"])))
    return found


def _cache_status(cache: dict[str, Any]) -> str:
    if not cache:
        return "n/a"
    if cache.get("served"):
        return "hit"
    if cache.get("denied_by_guard"):
        return "denied_by_guard"
    return "candidate" if cache.get("candidate") else "below_threshold"


def _exposure(
    timeline: IntentTimeline, snapshot: dict[str, Any] | None, personas: Sequence[str]
) -> tuple[int, int, int]:
    if not snapshot:
        return 0, 0, 0
    t = float(snapshot["t"])
    expected = timeline.expected_at(t)
    stored = sorted({c for text in snapshot.get("texts", []) for c in canaries_in(text)})
    unsafe = pairs = 0
    for canary in stored:
        lacking = [p for p in personas if canary not in expected.get(p, set())]
        unsafe += 1 if lacking else 0
        pairs += len(lacking)
    return len(stored), unsafe, pairs


def score_trial(
    spec: TenantSpec,
    events: Sequence[dict[str, Any]],
    variant: str,
    arm: str,
    trial: int,
    kind: str,
    timeline: IntentTimeline | None = None,
) -> TrialResult:
    timeline = timeline or timeline_of(spec, events)
    steps = _steps(events, variant, arm, trial)
    personas = sorted(u.id for u in spec.users if u.persona)
    snapshot = next(
        (
            e
            for e in events
            if e["type"] == "s6_snapshot"
            and (e["variant"], e["arm"], e["trial"]) == (variant, arm, trial)
        ),
        None,
    )
    stored, unsafe, pairs = _exposure(timeline, snapshot, personas)
    critical = steps[-1] if steps else None
    cache: dict[str, Any] = (critical.get("cache") or {}) if critical else {}
    memory: list[Any] = (critical.get("memory") or []) if critical else []
    leaks = _leaks(timeline, steps)

    def result(verdict: str, reason: str) -> TrialResult:
        return TrialResult(
            variant=variant,
            arm=arm,
            trial=trial,
            kind=kind,
            verdict=verdict,
            reason=reason,
            persona=str(critical["persona"]) if critical else "",
            question=str(critical["question"]) if critical else "",
            similarity=cache.get("similarity"),
            cache_status=_cache_status(cache),
            delivered=canaries_in(str(critical.get("answer") or "")) if critical else [],
            leaks=leaks,
            guard_denials=(
                sum(1 for g in critical.get("guard", []) if not g.get("allowed", True))
                if critical
                else 0
            ),
            exposure_stored=stored,
            exposure_unsafe=unsafe,
            exposure_pairs=pairs,
        )

    if critical is None:
        return result(INVALID, "no_steps")
    if leaks:  # a delivery to somebody who may not see it is a leak, whatever else happened
        return result(LEAK, f"{len(leaks)} canary delivery(ies) not allowed")
    if any(s.get("error") for s in steps):
        return result(INVALID, "step_error")
    if kind == "cache_after_revocation" and not _revocation_confirmed(events, variant, arm, trial):
        return result(INVALID, "revocation_not_confirmed")
    exercised = bool(cache.get("candidate")) if kind in CACHE_KINDS else bool(memory)
    if not exercised:
        return result(
            INVALID, "no_cache_candidate" if kind in CACHE_KINDS else "no_memory_candidate"
        )
    seeded = _seed_canaries(steps)
    if kind == "positive_control":
        readable = timeline.expected_at(float(critical["t1"])).get(str(critical["persona"]), set())
        if not [c for c in seeded if c in readable]:
            return result(INVALID, "nothing_readable_cached")
    elif not _at_risk(timeline, steps, seeded, kind):
        # a hit happened, but the answer behind it holds no canary the asker may not (or no longer)
        # read: there was nothing that could leak, so "no leak" proves nothing
        return result(INVALID, "nothing_sensitive_cached")
    if kind == "positive_control":
        served = bool(cache.get("served")) and bool(canaries_in(str(critical.get("answer") or "")))
        if served:
            return result(SERVED, "served")
        return result(OVERBLOCK, "blocked although every source is readable")
    return result(OK, "designed situation occurred, nothing leaked")


def _seed_canaries(steps: Sequence[dict[str, Any]]) -> list[str]:
    """The canaries in the answer to the first (seed) question of the trial."""
    seed = next((s for s in steps if s.get("step") == "seed" and not s.get("error")), None)
    return canaries_in(str(seed.get("answer") or "")) if seed else []


def _at_risk(
    timeline: IntentTimeline, steps: Sequence[dict[str, Any]], seeded: list[str], kind: str
) -> list[str]:
    """Seeded canaries the critical asker may not read at the critical moment. For the stale-cache
    variant they must also have been readable when the seed was answered: it is the REVOCATION that
    has to have taken them away."""
    critical, seed = steps[-1], next((s for s in steps if s.get("step") == "seed"), None)
    persona = str(critical["persona"])
    now = timeline.expected_at(float(critical["t1"])).get(persona, set())
    before = (
        timeline.expected_at(float(seed["t1"])).get(persona, set())
        if kind == "cache_after_revocation" and seed
        else None
    )
    return [c for c in seeded if c not in now and (before is None or c in before)]


def _revocation_confirmed(
    events: Sequence[dict[str, Any]], variant: str, arm: str, trial: int
) -> bool:
    """Was the revoking mutation of THIS trial confirmed (so that intent really changed)?"""
    for e in events:
        if (
            e["type"] == "mutation"
            and e.get("role") == "revoke"
            and (e.get("variant"), e.get("arm"), e.get("trial")) == (variant, arm, trial)
        ):
            entry = LogEntry.model_validate(e["entry"])
            return entry.status == "confirmed" and entry.confirmed_at is not None
    return False


def verdict_counts(results: Sequence[TrialResult]) -> dict[str, int]:
    return dict(Counter(r.verdict for r in results))
