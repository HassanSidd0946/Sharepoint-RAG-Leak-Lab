"""Glue between the runner and the generated tenant: canary -> drive item, persona -> UPN, and the
registry that builds a system under test by name.

Canary -> item: the generator knows each document's name and folder (Plan), so a canary maps to
(folder path, file name) and Graph's find_item turns that into a drive item id. Item ids survive a
move, so the ids are resolved once; a special file is looked up in the folder it is in NOW
(Env.special_folder, which apply_mutation keeps up to date).
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from leaklab.generator import Env
from leaklab.runner.model import RunnerError
from leaklab.runner.setup0 import Setup0SharePoint
from leaklab.runner.system import SystemUnderTest


def canary_locations(env: Env) -> dict[str, tuple[str, str]]:
    """canary string -> (folder path, file name), for every document of the plan."""
    out: dict[str, tuple[str, str]] = {}
    for folder in env.plan.folders:
        for doc in folder.docs:
            out[doc.canary] = (folder.path, doc.name)
    for special in env.plan.special_files:
        out[special.doc.canary] = (env.special_folder[special.id], special.doc.name)
    return out


def resolve_item_ids(env: Env, canaries: Sequence[str]) -> dict[str, str]:
    """canary -> drive item id (Graph). Raises if a document is missing: run generate first."""
    locations = canary_locations(env)
    ids: dict[str, str] = {}
    for canary in canaries:
        if canary not in locations:
            raise RunnerError(f"canary {canary!r} is not a document of this spec")
        folder, name = locations[canary]
        folder_id = env.folder_id(folder)
        item = None if folder_id is None else env.graph.find_item(env.drive_id, folder_id, name)
        if item is None:
            raise RunnerError(f"document {folder}/{name} (canary {canary}) does not exist")
        ids[canary] = str(item["id"])
    return ids


def persona_upns(env: Env) -> dict[str, str]:
    return {u.id: u.upn for u in env.plan.users if u.persona}


@dataclass(frozen=True)
class SystemContext:
    env: Env
    canaries: Sequence[str]  # every canary the run may read
    clock: Callable[[], float]


def build_setup0(ctx: SystemContext) -> SystemUnderTest:
    return Setup0SharePoint(
        drive_id=ctx.env.drive_id,
        item_ids=resolve_item_ids(ctx.env, ctx.canaries),
        upns=persona_upns(ctx.env),
        clock=ctx.clock,
    )


# Setup A (Azure AI Search) and Setup B (our overlay) register here when they exist.
SYSTEMS: dict[str, Callable[[SystemContext], SystemUnderTest]] = {"setup0": build_setup0}


def build_systems(names: Sequence[str], ctx: SystemContext) -> list[SystemUnderTest]:
    unknown = [n for n in names if n not in SYSTEMS]
    if unknown:
        raise RunnerError(f"unknown system(s) {unknown}; available: {sorted(SYSTEMS)}")
    return [SYSTEMS[n](ctx) for n in names]
