"""Glue between the runner and the generated tenant: canary -> drive item, persona -> UPN, and the
registry that builds a system under test by name.

Canary -> item: the generator knows each document's name and folder (Plan), so a canary maps to
(folder path, file name) and Graph's find_item turns that into a drive item id. Item ids survive a
move, so the ids are resolved once; a special file is looked up in the folder it is in NOW
(Env.special_folder, which apply_mutation keeps up to date).
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from leaklab.auth import get_persona_token
from leaklab.generator import Env
from leaklab.runner.model import RunnerError
from leaklab.runner.setup0 import Setup0SharePoint, TokenCache
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


def resolve_items(env: Env, canaries: Sequence[str]) -> dict[str, dict[str, Any]]:
    """canary -> its Graph driveItem. Raises if a document is missing: run generate first."""
    locations = canary_locations(env)
    items: dict[str, dict[str, Any]] = {}
    for canary in canaries:
        if canary not in locations:
            raise RunnerError(f"canary {canary!r} is not a document of this spec")
        folder, name = locations[canary]
        folder_id = env.folder_id(folder)
        item = None if folder_id is None else env.graph.find_item(env.drive_id, folder_id, name)
        if item is None:
            raise RunnerError(f"document {folder}/{name} (canary {canary}) does not exist")
        items[canary] = item
    return items


def resolve_item_ids(env: Env, canaries: Sequence[str]) -> dict[str, str]:
    """canary -> drive item id (Graph)."""
    return {c: str(item["id"]) for c, item in resolve_items(env, canaries).items()}


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


_SEARCH_TOKENS: dict[int, TokenCache] = {}


def search_token_cache(ctx: SystemContext) -> TokenCache:
    """ONE persona-token cache for the Azure AI Search scope per run, shared by Setup A and Setup B:
    Entra throttles repeated password sign-ins (AADSTS50196), so two caches would double them."""
    from leaklab.setup_a.client import SEARCH_SCOPE
    from leaklab.setup_a.system import TOKEN_MAX_AGE_S

    key = id(ctx)
    if key not in _SEARCH_TOKENS:
        _SEARCH_TOKENS[key] = TokenCache(
            lambda upn: get_persona_token(
                upn, [SEARCH_SCOPE], allow_device_code=False, use_cache=False
            ),
            ctx.clock,
            max_age_s=TOKEN_MAX_AGE_S,
        )
    return _SEARCH_TOKENS[key]


def build_setup_a_basic(ctx: SystemContext) -> SystemUnderTest:
    """Setup A without SharePoint-group support, provisioned with the smallest indexer schedule."""
    import httpx

    from leaklab.auth import get_app_token
    from leaklab.config import get_settings
    from leaklab.setup_a.client import SEARCH_SCOPE, HttpSearchPort
    from leaklab.setup_a.provision import datasource_body, ensure_provisioned
    from leaklab.setup_a.system import SetupABasic

    s = get_settings()
    endpoint = s.search_endpoint.strip() or saved_endpoint()
    if not endpoint:
        raise RunnerError("no search endpoint: set LEAKLAB_SEARCH_ENDPOINT or run search_provision")
    env = ctx.env
    folder = f"https://{s.sp_host.strip()}{env.library_url}/{env.plan.container}"
    secret = s.ingest_app_secret.get_secret_value()
    with httpx.Client(timeout=60.0) as http:
        done = ensure_provisioned(
            http,
            endpoint,
            get_app_token(SEARCH_SCOPE),
            datasource_body(
                site_url=s.site_url.strip(),
                app_id=s.ingest_app_id.strip(),
                secret=secret,
                tenant_id=s.tenant_id.strip(),
                folder_url=folder,
            )
            if secret and s.ingest_app_id.strip()
            else {},
        )
    env.event("setup_a_provisioned", **done)
    items = resolve_items(env, ctx.canaries)
    created = {
        c: datetime.fromisoformat(str(i["createdDateTime"]).replace("Z", "+00:00")).timestamp()
        for c, i in items.items()
        if i.get("createdDateTime")
    }
    tokens = search_token_cache(ctx)
    return SetupABasic(
        search=HttpSearchPort(endpoint, clock=ctx.clock),
        upns=persona_upns(env),
        tokens=tokens,
        created_at=created,
        clock=ctx.clock,
    )


def build_setup_b(ctx: SystemContext) -> SystemUnderTest:
    """Setup B: pollers + API in this process, read through HTTP like a client.

    A FRESH state schema per run (its first full sync takes about 40 s), dropped at close, so a run
    never starts from the previous run's state; the decision log keeps its own schema and so keeps
    one chain across runs. The server binds 127.0.0.1, with debug stats on (benchmark only).
    """
    import secrets
    import threading
    from contextlib import ExitStack

    from leaklab.setup_b.api.app import create_app
    from leaklab.setup_b.api.server import LocalServer, build_service
    from leaklab.setup_b.state.run import open_stores, start_pollers
    from leaklab.setup_b.system import SetupB

    stack = ExitStack()
    try:
        stores = stack.enter_context(open_stores(schema=f"leaklab_run_{int(ctx.clock())}"))
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
        api = create_app(service, operator_key=secrets.token_hex(16), allow_debug=True)
        server = stack.enter_context(LocalServer(api))
        tokens = search_token_cache(ctx)
        return SetupB(
            base_url=server.url,
            upns=persona_upns(ctx.env),
            tokens=tokens,
            clock=ctx.clock,
            sleep=ctx.env.sleep,
            indexer_runs=service.indexer_runs,
            closers=[stack.close],
        )
    except BaseException:
        stack.close()
        raise


def saved_endpoint() -> str:
    """The endpoint search_provision.py saved under runs/ (git-ignored), or ''."""
    import json
    from pathlib import Path

    path = Path(__file__).resolve().parents[3] / "runs" / "search_service.json"
    return (
        str(json.loads(path.read_text(encoding="utf-8")).get("endpoint", ""))
        if path.exists()
        else ""
    )


SYSTEMS: dict[str, Callable[[SystemContext], SystemUnderTest]] = {
    "setup0": build_setup0,
    "setup_a_basic": build_setup_a_basic,
    "setup_b": build_setup_b,
}


def build_systems(names: Sequence[str], ctx: SystemContext) -> list[SystemUnderTest]:
    unknown = [n for n in names if n not in SYSTEMS]
    if unknown:
        raise RunnerError(f"unknown system(s) {unknown}; available: {sorted(SYSTEMS)}")
    return [SYSTEMS[n](ctx) for n in names]
