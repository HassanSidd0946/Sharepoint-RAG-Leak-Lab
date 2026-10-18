"""Probe Azure AI Search + the SharePoint indexer's ACL ingestion (preview). NOT setup_a/.

Follows Microsoft's docs (api-version 2026-08-01-preview, read 2026-10-17):
  search-indexer-sharepoint-access-control-lists, search-how-to-index-sharepoint-online,
  search-query-access-control-rbac-enforcement.
Everything the docs do not state is recorded as UNVERIFIED in the output, not guessed.

Stages (run in this order; each writes a timestamped probes/out/search_acl_<stage>_<UTC>.json):
  preflight  can the driver app (Entra token, scope https://search.azure.com/.default) reach the service?
  setup      data source (type sharepoint, indexerPermissionOptions userIds+groupIds, container =
             the ll-lab folder only), index (permission fields + permissionFilterOption enabled),
             indexer (field mappings metadata_user_ids/metadata_group_ids). No SharePoint-group
             configuration: that needs a federated credential (see the docs); this stage records what
             the plain configuration does with a SharePoint-group grant.
  run        run the indexer, poll its status, record the duration and counts.
  dump       elevated-read query (x-ms-enable-elevated-read) of every document with UserIds/GroupIds:
             how each grant type is stored (Entra group id, direct user id, SharePoint group: ?).
  trim       query as p-plateng and p-intern with THEIR token in x-ms-query-source-authorization:
             which canaries come back, compared with the oracle (leaks, lockouts); one canary each.
  revoke     --arm entra|direct|spgroup: apply the S1 scenario's mutation WITHOUT running the indexer,
             poll the query as p-plateng with an OLD token (taken before) and a FRESH one: is group
             membership evaluated at query time or frozen? With --rerun, then run the indexer and
             (with --resync) call resync options=["permissions"], recording what picks the change up.

Secrets: the ingestion app's secret goes only into the data source request body; recorded request
bodies have it replaced by "<redacted>". Tokens are never printed or saved. Test tenant only.

Setup (once): uv run python probes/search_provision.py ; then put LEAKLAB_SEARCH_ENDPOINT,
LEAKLAB_INGEST_APP_ID, LEAKLAB_INGEST_APP_SECRET in .env; uv run python -m leaklab.generator generate
"""

import json
import re
import time
import traceback
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import typer
from rich.console import Console

from leaklab.auth import describe_token, get_app_token, get_persona_token
from leaklab.config import get_settings
from leaklab.generator import Env, apply_mutation
from leaklab.generator.cli import open_env
from leaklab.oracle import Mutation, expected_access
from leaklab.runner.scenarios import scenario_mutations
from leaklab.spec import generate_canaries, load_spec

ROOT = Path(__file__).resolve().parents[1]
SPEC_PATH = ROOT / "tenant_spec.yaml"
OUT = ROOT / "probes" / "out"
API = "2026-08-01-preview"
# UNVERIFIED: the docs say the query token is "a Microsoft Entra access token for the querying user"
# and do not name its audience here. The stage "trim" records what the service answers to this one.
SEARCH_SCOPE = "https://search.azure.com/.default"
DATASOURCE, INDEX, INDEXER = "ll-sp-datasource", "ll-sp-index", "ll-sp-indexer"
# canaries of a tenant generation: CANARY-G<6>-<code>-<nn>-<hex6> and CANARY-G<6>-SPC-<hex6>; the older
# form without the generation (runs before 2026-10-18) is still recognized
CANARY_RE = re.compile(r"CANARY-(?:G[0-9A-Z]{6}-)?(?:[A-Z0-9]+-\d{2}|SPC)-[0-9a-f]{6}")
console = Console()
app = typer.Typer(add_completion=False, no_args_is_help=True)

ARMS = {"entra": "S1-entra", "direct": "S1-direct", "spgroup": "S1-spgroup"}
ARM_FOLDER = {"entra": "S1-Entra", "direct": "S1-Direct", "spgroup": "S1-SPGroup"}


class Recorder:
    """Collects every step (what was asked, what came back) and writes one JSON file at the end."""

    def __init__(self, stage: str) -> None:
        self.stage = stage
        self.data: dict[str, Any] = {
            "stage": stage,
            "started_utc": datetime.now(UTC).isoformat(),
            "api_version": API,
            "steps": [],
        }

    def step(self, name: str, **info: Any) -> dict[str, Any]:
        entry = {"step": name, "t": round(time.time(), 3), **info}
        self.data["steps"].append(entry)
        short = {k: v for k, v in info.items() if k in ("http", "result", "note", "count")}
        console.print(f"[bold]{name}[/bold] {short}")
        return entry

    def save(self) -> Path:
        OUT.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        path = OUT / f"search_acl_{self.stage}_{stamp}.json"
        path.write_text(json.dumps(self.data, indent=2, default=str) + "\n", encoding="utf-8")
        console.print(f"saved {path}")
        return path


class Search:
    """The service's data plane. Admin calls use the DRIVER app's Entra token (RBAC roles from
    search_provision.py); a user's identity travels in x-ms-query-source-authorization."""

    def __init__(self, endpoint: str) -> None:
        self.endpoint = endpoint.rstrip("/")
        self._http = httpx.Client(timeout=120.0)
        self._token: tuple[str, float] | None = None

    def driver_token(self) -> str:
        if self._token is None or self._token[1] < time.time() + 300:
            token = get_app_token(SEARCH_SCOPE)
            exp = describe_token(token).get("exp")
            self._token = (token, float(exp) if isinstance(exp, int | float) else time.time() + 600)
        return self._token[0]

    def call(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, Any]:
        sep = "&" if "?" in path else "?"
        url = f"{self.endpoint}{path}{sep}api-version={API}"
        hdrs = {"Authorization": f"Bearer {self.driver_token()}", **(headers or {})}
        for attempt in range(4):
            try:
                resp = self._http.request(method, url, json=body, headers=hdrs)
            except httpx.TransportError as exc:
                return 0, f"transport:{type(exc).__name__}"
            if resp.status_code in (429, 503) and attempt < 3:
                time.sleep(float(resp.headers.get("Retry-After", 2 * (attempt + 1))))
                continue
            try:
                return resp.status_code, resp.json() if resp.content else None
            except ValueError:
                return resp.status_code, resp.text[:500]
        return 0, "unreachable"


def excerpt(value: Any, limit: int = 1200) -> Any:
    text = json.dumps(value, default=str)
    return value if len(text) <= limit else text[:limit] + "...<cut>"


def endpoint_from_config() -> str:
    endpoint = get_settings().search_endpoint.strip()
    if not endpoint:
        saved = ROOT / "runs" / "search_service.json"
        if saved.exists():
            endpoint = str(json.loads(saved.read_text(encoding="utf-8"))["endpoint"])
    if not endpoint:
        raise typer.BadParameter(
            "No search endpoint: set LEAKLAB_SEARCH_ENDPOINT or run probes/search_provision.py."
        )
    return endpoint


def ids_to_spec(env: Env) -> dict[str, str]:
    """Entra object id -> spec id, for users and groups (so stored ACL values can be read)."""
    out = {env.user_id(u.id): f"user:{u.id}" for u in env.plan.users}
    out.update({env.group_id(g.id): f"group:{g.id}" for g in env.plan.groups})
    return out


def label_acl(values: list[str] | None, names: dict[str, str]) -> list[str]:
    """Stored ACL values as spec ids; 'spg:' values and anything unknown stay raw and marked."""
    out = []
    for v in values or []:
        out.append(
            names.get(v)
            or (f"SHAREPOINT-GROUP-ID:{v}" if v.startswith("spg:") else f"UNMAPPED:{v}")
        )
    return sorted(out)


def canaries_in(text: str | None) -> list[str]:
    return sorted(set(CANARY_RE.findall(text or "")))


def acl_by_folder(docs: list[dict[str, Any]], names: dict[str, str]) -> dict[str, Any]:
    """folder code in the canary (S1D, GEN, ...) -> the distinct stored UserIds/GroupIds lists."""
    out: dict[str, list[dict[str, list[str]]]] = {}
    for doc in docs:
        found = canaries_in(doc.get("content"))
        code = found[0].split("-")[1] if found else "?"
        entry = {
            "UserIds": label_acl(doc.get("UserIds"), names),
            "GroupIds": label_acl(doc.get("GroupIds"), names),
        }
        if entry not in out.setdefault(code, []):
            out[code].append(entry)
    return out


def compare_with_oracle(
    returned: set[str], persona: str, expected: dict[str, set[str]]
) -> dict[str, Any]:
    want = expected.get(persona, set())
    return {
        "returned": len(returned),
        "expected": len(want),
        "leaks": sorted(returned - want),
        "lockouts": sorted(want - returned),
        "ok": returned == want,
    }


def all_docs(
    search: Search,
    headers: dict[str, str],
    select: str,
    elevated: bool = False,
    odata: bool = False,
) -> tuple[int, Any]:
    """Every document. `odata=True` uses the path the docs show for elevated reads
    (/indexes('name')/search.post.search); it answered HTTP 405 on 2026-10-17, so the default is the
    ordinary /indexes/{name}/docs/search with the same header."""
    body = {"search": "*", "select": select, "top": 1000, "count": True}
    path = f"/indexes('{INDEX}')/search.post.search" if odata else f"/indexes/{INDEX}/docs/search"
    if elevated:
        headers = {**headers, "x-ms-enable-elevated-read": "true"}
    return search.call("POST", path, body, headers)


def user_token(upn: str) -> str:
    return get_persona_token(upn, [SEARCH_SCOPE], allow_device_code=False, use_cache=False)


def persona_query(search: Search, upn: str, token: str, query: str = "*") -> tuple[int, Any]:
    body = {"search": query, "select": "metadata_spo_item_name,content", "top": 1000, "count": True}
    return search.call(
        "POST", f"/indexes/{INDEX}/docs/search", body, {"x-ms-query-source-authorization": token}
    )


def returned_canaries(status: int, data: Any) -> set[str] | None:
    """The canaries in a successful answer; None when the call did not succeed (never 'empty')."""
    if status != 200 or not isinstance(data, dict):
        return None
    return {c for doc in data.get("value", []) for c in canaries_in(doc.get("content"))}


def guarded(stage: str, fn: Callable[[Recorder], None]) -> None:
    rec = Recorder(stage)
    try:
        fn(rec)
    except Exception:  # noqa: BLE001 - the probe keeps its evidence even when it fails
        rec.data["error"] = traceback.format_exc()
        console.print(rec.data["error"])
    finally:
        rec.save()


# -- stages ---------------------------------------------------------------------------------------


@app.command()
def preflight() -> None:
    """Reach the service with the driver app's Entra token."""

    def run(rec: Recorder) -> None:
        search = Search(endpoint_from_config())
        for name, path in (("servicestats", "/servicestats"), ("indexes", "/indexes?$select=name")):
            status, data = search.call("GET", path)
            rec.step(name, http=status, result=excerpt(data))
        rec.step(
            "driver token",
            note="claims only",
            claims=describe_token(search.driver_token()),
        )

    guarded("preflight", run)


@app.command()
def setup() -> None:
    """Create the data source, index and indexer (ll-lab only, permission fields on)."""

    def run(rec: Recorder) -> None:
        s = get_settings()
        if not s.ingest_app_id.strip() or not s.ingest_app_secret.get_secret_value():
            raise SystemExit(
                "Set LEAKLAB_INGEST_APP_ID and LEAKLAB_INGEST_APP_SECRET (see .env.example)."
            )
        search = Search(endpoint_from_config())
        with open_env(SPEC_PATH) as env:
            # the docs example omits the scheme; the service answered "Invalid URI format" without it (2026-10-17)
            folder = f"https://{s.sp_host.strip()}{env.library_url}/{env.plan.container}"
        connection = (
            f"SharePointOnlineEndpoint={s.site_url.strip()};ApplicationId={s.ingest_app_id.strip()};"
            f"ApplicationSecret={s.ingest_app_secret.get_secret_value()};TenantId={s.tenant_id.strip()}"
        )
        datasource = {
            "name": DATASOURCE,
            "type": "sharepoint",
            "indexerPermissionOptions": ["userIds", "groupIds"],
            "credentials": {"connectionString": connection},
            "container": {"name": "useQuery", "query": f"includeFolder={folder}"},
        }
        status, data = search.call("PUT", f"/datasources/{DATASOURCE}", datasource)
        shown = {**datasource, "credentials": {"connectionString": "<redacted>"}}
        rec.step("create data source", http=status, request=shown, result=excerpt(data),
                 unverified="SharePointOnlineEndpoint = the SITE url (docs example shows the tenant root)")  # fmt: skip
        index = {
            "name": INDEX,
            "fields": [
                {"name": "id", "type": "Edm.String", "key": True, "searchable": False},
                {"name": "metadata_spo_item_name", "type": "Edm.String", "searchable": True, "retrievable": True},
                {"name": "metadata_spo_item_path", "type": "Edm.String", "searchable": False, "retrievable": True},
                {"name": "content", "type": "Edm.String", "searchable": True, "retrievable": True},
                {"name": "UserIds", "type": "Collection(Edm.String)", "permissionFilter": "userIds", "filterable": True, "retrievable": True},
                {"name": "GroupIds", "type": "Collection(Edm.String)", "permissionFilter": "groupIds", "filterable": True, "retrievable": True},
            ],
            "permissionFilterOption": "enabled",
        }  # fmt: skip
        status, data = search.call("PUT", f"/indexes/{INDEX}", index)
        rec.step("create index", http=status, request=index, result=excerpt(data),
                 note="retrievable=true on the ACL fields is for this probe only (docs: development only)")  # fmt: skip
        indexer = {
            "name": INDEXER,
            "dataSourceName": DATASOURCE,
            "targetIndexName": INDEX,
            "parameters": {
                "configuration": {
                    "indexedFileNameExtensions": ".txt",
                    "dataToExtract": "contentAndMetadata",
                    "failOnUnsupportedContentType": False,
                }
            },
            "fieldMappings": [
                {"sourceFieldName": "metadata_spo_site_library_item_id", "targetFieldName": "id",
                 "mappingFunction": {"name": "base64Encode"}},
                {"sourceFieldName": "metadata_user_ids", "targetFieldName": "UserIds"},
                {"sourceFieldName": "metadata_group_ids", "targetFieldName": "GroupIds"},
            ],
        }  # fmt: skip
        status, data = search.call("PUT", f"/indexers/{INDEXER}", indexer)
        rec.step("create indexer", http=status, request=indexer, result=excerpt(data))

    guarded("setup", run)


def run_indexer(
    search: Search, rec: Recorder, label: str, timeout_s: float = 900.0
) -> dict[str, Any]:
    """Run the indexer once and poll until it ends. Returns the final lastResult plus our timings."""
    before_status, before = search.call("GET", f"/indexers/{INDEXER}/status")
    previous_end = (
        (before or {}).get("lastResult", {}).get("endTime") if isinstance(before, dict) else None
    )
    t0 = time.time()
    status, data = search.call("POST", f"/indexers/{INDEXER}/run")
    rec.step(f"{label}: run", http=status, result=excerpt(data))
    last: dict[str, Any] = {}
    while time.time() - t0 < timeout_s:
        time.sleep(5)
        status, data = search.call("GET", f"/indexers/{INDEXER}/status")
        last = (data or {}).get("lastResult") or {} if isinstance(data, dict) else {}
        if last and last.get("status") != "inProgress" and last.get("endTime") != previous_end:
            break
    result = {
        "label": label,
        "wall_s": round(time.time() - t0, 1),
        "status": last.get("status"),
        "startTime": last.get("startTime"),
        "endTime": last.get("endTime"),
        "itemsProcessed": last.get("itemsProcessed"),
        "itemsFailed": last.get("itemsFailed"),
        "errorMessage": last.get("errorMessage"),
        "errors": excerpt(last.get("errors")),
        "warnings": excerpt(last.get("warnings")),
    }
    rec.step(f"{label}: finished", **result)
    return result


@app.command()
def run() -> None:
    """Run the indexer once; record its duration and counts."""
    guarded("run", lambda rec: run_indexer(Search(endpoint_from_config()), rec, "initial run"))


@app.command()
def dump() -> None:
    """Elevated read of every document: how each grant type is stored."""

    def stage(rec: Recorder) -> None:
        search = Search(endpoint_from_config())
        with open_env(SPEC_PATH) as env:
            names = ids_to_spec(env)
        select = "metadata_spo_item_name,content,UserIds,GroupIds"
        # Verified 2026-10-17: the service refuses x-ms-enable-elevated-read with an app-only token
        # ("requires an AAD user access token") and refuses it together with a user token in
        # x-ms-query-source-authorization. So the ADMIN signs in (device code) and her token is the
        # Authorization bearer; the admin needs the role Search Index Data Contributor on the service.
        admin = get_persona_token(
            get_settings().admin_upn,
            [SEARCH_SCOPE],
            use_password=False,
            allow_device_code=True,
            use_cache=False,
        )
        rec.step("admin token", note="claims only", claims=describe_token(admin))
        auth = {"Authorization": f"Bearer {admin}"}
        # Diagnose a 403 before the elevated read: does the admin have ANY data-plane role here?
        # (listing indexes needs a data-plane role; the elevated read also needs the "elevated read"
        # permission that Search Index Data Contributor includes, per the docs)
        status, data = search.call("GET", "/indexes?$select=name", None, auth)
        rec.step("admin: list indexes (any data-plane role?)", http=status, result=excerpt(data))
        rec.step("endpoint used", note=search.endpoint)
        status, data = all_docs(search, auth, select, elevated=True)
        rec.step(
            "elevated read as the admin user",
            http=status,
            count=(data or {}).get("@odata.count") if isinstance(data, dict) else None,
            result=excerpt(data) if status != 200 else "ok",
        )
        if status == 200 and isinstance(data, dict):
            docs = data.get("value", [])
            rec.step("documents", count=len(docs))
            rec.data["acl_by_folder_code"] = acl_by_folder(docs, names)
            rec.data["sample_document"] = excerpt(docs[0] if docs else None)
            console.print(json.dumps(rec.data["acl_by_folder_code"], indent=1)[:3000])

    guarded("dump", stage)


@app.command()
def trim(personas: str = "p-plateng,p-intern") -> None:
    """Query as each persona with their own token and compare with the oracle."""

    def stage(rec: Recorder) -> None:
        search = Search(endpoint_from_config())
        spec = load_spec(SPEC_PATH)
        canaries = generate_canaries(spec)
        expected = expected_access(spec, [], 0.0, canaries=canaries)
        upns = {u.id: f"{spec.naming.prefix}{u.id}@{spec.naming.domain}" for u in spec.users}
        for persona in [p.strip() for p in personas.split(",") if p.strip()]:
            try:
                token = user_token(upns[persona])
            except Exception as exc:  # noqa: BLE001
                rec.step(f"{persona}: token for {SEARCH_SCOPE}", result="FAILED", note=f"{type(exc).__name__}: {str(exc)[:500]}")  # fmt: skip
                continue
            rec.step(f"{persona}: token", note="claims only", claims=describe_token(token))
            status, data = persona_query(search, upns[persona], token)
            got = returned_canaries(status, data)
            rec.step(
                f"{persona}: search *", http=status, result=excerpt(data) if got is None else "ok"
            )
            if got is not None:
                rec.step(f"{persona}: vs oracle", **compare_with_oracle(got, persona, expected))
            for label, canary in (("S1-Direct doc 1", canaries["S1-Direct"][0]), ("General doc 1", canaries["General"][0])):  # fmt: skip
                status, data = persona_query(search, upns[persona], token, f'"{canary}"')
                found = returned_canaries(status, data)
                rec.step(f"{persona}: query one canary ({label})", http=status, returned=None if found is None else canary in found)  # fmt: skip
            rec.step(f"{persona}: no token at all", note="only public documents expected")
            status, data = search.call("POST", f"/indexes/{INDEX}/docs/search", {"search": "*", "select": "metadata_spo_item_name", "top": 5, "count": True})  # fmt: skip
            rec.step(f"{persona}: query WITHOUT x-ms-query-source-authorization", http=status, result=excerpt(data))  # fmt: skip

    guarded("trim", stage)


@app.command()
def notoken() -> None:
    """What does the service return with the app token only (no user identity)? Needs no consent."""

    def stage(rec: Recorder) -> None:
        search = Search(endpoint_from_config())
        status, data = search.call("GET", f"/indexes/{INDEX}/stats")
        rec.step("index statistics", http=status, result=excerpt(data))
        status, data = search.call(
            "POST",
            f"/indexes/{INDEX}/docs/search",
            {"search": "*", "select": "metadata_spo_item_name,content", "top": 1000, "count": True},
        )
        got = returned_canaries(status, data)
        count = data.get("@odata.count") if isinstance(data, dict) else None
        rec.step(
            "search * with the app token only (no x-ms-query-source-authorization)",
            http=status,
            count=count,
            canaries_returned=None if got is None else len(got),
            result=excerpt(data) if got is None or got else "ok",
        )

    guarded("notoken", stage)


def admin_key(search: Search, rec: Recorder) -> str | None:
    """The service's ADMIN key, never printed or saved. First LEAKLAB_SEARCH_KEY (checked against
    this endpoint: a key is per service); else the management plane (listAdminKeys), which the
    driver app may call because search_provision.py gave it Search Service Contributor."""
    key = get_settings().search_key.get_secret_value()
    if key:
        resp = httpx.get(
            f"{search.endpoint}/servicestats?api-version={API}",
            headers={"api-key": key},
            timeout=60,
        )
        rec.step("admin key from LEAKLAB_SEARCH_KEY", http=resp.status_code, note="403 = the key belongs to another service")  # fmt: skip
        if resp.status_code == 200:
            return key
    saved = ROOT / "runs" / "search_service.json"
    if not saved.exists():
        return None
    info = json.loads(saved.read_text(encoding="utf-8"))
    token = get_app_token("https://management.azure.com/.default")
    url = (
        f"https://management.azure.com/subscriptions/{info['subscription_id']}"
        f"/resourceGroups/{info['resource_group']}/providers/Microsoft.Search"
        f"/searchServices/{info['service']}/listAdminKeys?api-version=2023-11-01"
    )
    resp = httpx.post(url, headers={"Authorization": f"Bearer {token}"}, timeout=60)
    rec.step("admin key from listAdminKeys (driver app, management plane)", http=resp.status_code)
    return str(resp.json().get("primaryKey")) if resp.status_code == 200 else None


@app.command()
def lookup() -> None:
    """Probe (a): can a document be fetched BY KEY with the admin key while ACL filtering is on?"""

    def stage(rec: Recorder) -> None:
        from leaklab.graph_client import GraphClient
        from leaklab.runner.tenant import resolve_item_ids
        from leaklab.setup_a.keys import doc_key

        s = get_settings()
        search = Search(endpoint_from_config())
        spec = load_spec(SPEC_PATH)
        canaries = generate_canaries(spec)
        general, board = canaries["General"][0], canaries["Board"][0]
        with GraphClient() as gc, open_env(SPEC_PATH) as env:
            site = gc.get(f"/sites/{s.sp_host.strip()}:{urlsplit(s.site_url).path.rstrip('/')}")
            drive = gc.get(f"/sites/{site['id']}/drive")
            items = resolve_item_ids(env, [general, board])
        keys = {c: doc_key(str(site["id"]), str(drive["id"]), items[c]) for c in (general, board)}
        # the key as the INDEX stores it, read as p-intern (who may read General)
        intern = f"{spec.naming.prefix}p-intern@{spec.naming.domain}"
        ptoken = user_token(intern)
        body = {"search": f'"{general}"', "select": "id,metadata_spo_item_name", "top": 1}
        status, data = search.call("POST", f"/indexes/{INDEX}/docs/search", body, {"x-ms-query-source-authorization": ptoken})  # fmt: skip
        stored = (data or {}).get("value", [{}])[0].get("id") if isinstance(data, dict) else None
        rec.step("derived key equals the key the index stores (General doc 1)", http=status, equal=stored == keys[general], note="UNVERIFIED before this run")  # fmt: skip
        key_for_api = admin_key(search, rec)
        rec.step("admin key obtained", result=key_for_api is not None)
        bearer = {"Authorization": f"Bearer {search.driver_token()}"}
        ceo = f"{spec.naming.prefix}p-ceo@{spec.naming.domain}"
        ctoken = user_token(ceo)
        cases: list[tuple[str, str, dict[str, str]]] = []
        for label, canary in (("General doc (p-intern may read)", general), ("Board doc (p-intern may NOT read)", board)):  # fmt: skip
            if key_for_api:
                cases.append(
                    (f"{label}: admin api-key only", keys[canary], {"api-key": key_for_api})
                )
            cases += [
                (f"{label}: driver bearer only", keys[canary], bearer),
                (
                    f"{label}: driver bearer + p-intern token",
                    keys[canary],
                    {**bearer, "x-ms-query-source-authorization": ptoken},
                ),  # fmt: skip
                (
                    f"{label}: driver bearer + p-ceo token",
                    keys[canary],
                    {**bearer, "x-ms-query-source-authorization": ctoken},
                ),  # fmt: skip
            ]
        cases.append(("a key that does not exist: driver bearer + p-intern token", doc_key("x", "y", "z"), {**bearer, "x-ms-query-source-authorization": ptoken}))  # fmt: skip
        if key_for_api:
            cases.append(("a key that does not exist: admin api-key only", doc_key("x", "y", "z"), {"api-key": key_for_api}))  # fmt: skip
        http = httpx.Client(timeout=60)
        for label, key, headers in cases:
            resp = http.get(
                f"{search.endpoint}/indexes/{INDEX}/docs/{key}",
                params={"api-version": API, "$select": "metadata_spo_item_name"},
                headers=headers,
            )
            rec.step(f"lookup: {label}", http=resp.status_code, result=resp.text[:200])

    guarded("lookup", stage)


@app.command()
def schedule() -> None:
    """Probe (b): the smallest indexer schedule the service accepts (docs: 5 minutes)."""

    def stage(rec: Recorder) -> None:
        search = Search(endpoint_from_config())
        status, current = search.call("GET", f"/indexers/{INDEXER}")
        rec.step("current indexer", http=status, schedule=(current or {}).get("schedule") if isinstance(current, dict) else None)  # fmt: skip
        if not isinstance(current, dict):
            return
        for interval in ("PT1M", "PT2M", "PT4M", "PT5M"):
            body = {k: v for k, v in current.items() if not k.startswith("@")}
            body["schedule"] = {"interval": interval}
            status, data = search.call("PUT", f"/indexers/{INDEXER}", body)
            rec.step(f"set schedule {interval}", http=status, result=excerpt(data) if status >= 300 else "accepted")  # fmt: skip
            if status < 300:
                rec.data["smallest_accepted"] = interval
                break
        status, after = search.call("GET", f"/indexers/{INDEXER}")
        rec.step("indexer after", http=status, schedule=(after or {}).get("schedule") if isinstance(after, dict) else None)  # fmt: skip

    guarded("schedule", stage)


@app.command()
def revoke(
    arm: str = typer.Option(..., help="entra | direct | spgroup"),
    minutes: float = typer.Option(10.0, help="How long to poll after the change."),
    interval_s: float = typer.Option(10.0),
    rerun: bool = typer.Option(False, help="Then run the indexer (incremental) and keep polling."),
    resync: bool = typer.Option(False, help="Then also call resync options=[permissions] + run."),
    rerun_after_s: float = typer.Option(240.0, help="Seconds after the change to run the indexer."),
    resync_after_s: float = typer.Option(480.0, help="Seconds after the change to resync."),
    fresh_every_s: float = typer.Option(
        120.0, help="New 'fresh' token this often (Entra throttles password sign-ins: AADSTS50196)."
    ),
) -> None:
    """Change the tenant for one S1 arm WITHOUT running the indexer; see what the query does."""

    def stage(rec: Recorder) -> None:
        if arm not in ARMS:
            raise SystemExit(f"--arm must be one of {sorted(ARMS)}")
        search = Search(endpoint_from_config())
        spec = load_spec(SPEC_PATH)
        scenario = next(s for s in spec.scenarios if s.id == ARMS[arm])
        arm_canaries = set(generate_canaries(spec)[ARM_FOLDER[arm]])
        upn = f"{spec.naming.prefix}p-plateng@{spec.naming.domain}"
        old_token = user_token(upn)

        def serving(token: str) -> bool | None:
            status, data = persona_query(search, upn, token)
            got = returned_canaries(status, data)
            return None if got is None else bool(got & arm_canaries)

        baseline = serving(old_token)
        rec.step("baseline: arm documents served to p-plateng", result=baseline)
        if baseline is not True:
            rec.step("ABORT", note="the arm is not served before the change; nothing to revoke")
            return
        mutations = scenario_mutations(scenario)
        with open_env(SPEC_PATH) as env:
            entry = apply_mutation(
                env, Mutation(type=mutations[0].type, params=mutations[0].params)
            )
        confirmed = entry.confirmed_at or time.time()
        rec.step("mutation applied", note=f"{scenario.id}; the indexer has NOT been run", status=entry.status, confirmed_at=confirmed)  # fmt: skip
        samples: list[dict[str, Any]] = []
        rec.data["samples"] = samples
        phase, rerun_at, resync_at = "after change (no indexer)", None, None
        end = confirmed + minutes * 60
        fresh, fresh_at, fresh_error = old_token, 0.0, None
        while time.time() < end:
            if time.time() - fresh_at >= fresh_every_s:
                try:
                    fresh, fresh_at, fresh_error = user_token(upn), time.time(), None
                except Exception as exc:  # noqa: BLE001 - a failed sign-in is an error poll
                    fresh, fresh_at, fresh_error = "", time.time(), type(exc).__name__
            row = {
                "t_s": round(time.time() - confirmed, 1),
                "phase": phase,
                "old_token": serving(old_token),
                "fresh_token": serving(fresh) if fresh else None,
                "fresh_token_error": fresh_error,
            }
            samples.append(row)
            time.sleep(interval_s)
            quiet = samples[-6:]
            if (
                not rerun
                and len(quiet) == 6
                and all(r["old_token"] is False and r["fresh_token"] is False for r in quiet)
            ):
                rec.step("stop", note="neither token served the arm for 6 polls in a row")
                break
            if rerun and rerun_at is None and time.time() - confirmed > rerun_after_s:
                phase, rerun_at = "after indexer run", time.time() - confirmed
                rec.step(
                    "indexer", result=run_indexer(search, rec, "incremental run after the change")
                )
            if (
                resync
                and rerun_at is not None
                and resync_at is None
                and time.time() - confirmed > resync_after_s
            ):
                phase, resync_at = "after resync + run", time.time() - confirmed
                status, data = search.call(
                    "POST", f"/indexers/{INDEXER}/resync", {"options": ["permissions"]}
                )
                rec.step("resync permissions", http=status, result=excerpt(data))
                rec.step("indexer", result=run_indexer(search, rec, "run after resync"))
        served = [r for r in samples if r["fresh_token"] or r["old_token"]]
        rec.data["summary"] = {
            "arm": arm,
            "polls": len(samples),
            "polls_still_served_by_either_token": len(served),
            "first_poll_where_neither_token_is_served_s": next(
                (
                    r["t_s"]
                    for r in samples
                    if r["old_token"] is False and r["fresh_token"] is False
                ),
                None,
            ),  # fmt: skip
        }
        console.print(rec.data["summary"])

    guarded(f"revoke_{arm}", stage)


if __name__ == "__main__":
    app()
