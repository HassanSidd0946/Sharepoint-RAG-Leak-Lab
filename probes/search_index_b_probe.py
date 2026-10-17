"""Probe for Setup B's own index: ll-sp-index-b, an UNFILTERED copy of ll-sp-index (prompt 19b).

Why: the app-only (service) token sees 0 documents in ll-sp-index because permissionFilterOption is
enabled (docs/FINDINGS.md, Setup A probes), so Setup B cannot post-filter anything there. Decision
(user, 2026-10-17): a second index over the SAME SharePoint data source, identical to ll-sp-index in
fields, analyzers and indexer schedule, differing only in permissionFilterOption. Any other
difference the service forces is recorded as a caveat in the output.

Stages (each writes probes/out/search_acl_<stage>_<UTC>.json via search_acl_probe.Recorder):
  index_b_create  create the index (variants tried in order: only permissionFilterOption changed; then
                  also without the permissionFilter attributes), create the second indexer (same data
                  source, same schedule), wait for a finished run, then query WITH THE SERVICE TOKEN ONLY:
                  how many documents come back, is the key the same as in ll-sp-index, what is in the ACL
                  fields (shown for comparison only: Setup B never decides from them).
  token           fetch the tenant's signing keys (JWKS) and verify a real persona token (signature, issuer,
                  tenant, audience) with PyJWT: what the service's /query validation can rely on.

Run: uv run python probes/search_index_b_probe.py index-b-create
     uv run python probes/search_index_b_probe.py token
Test tenant only. Tokens are never printed or saved; only decoded non-secret claims.
"""

import copy
import time
from typing import Any

import httpx
import jwt
import typer

from leaklab.auth import describe_token, get_persona_token
from leaklab.config import get_settings
from leaklab.generator.cli import open_env
from leaklab.setup_a.client import SEARCH_SCOPE
from leaklab.setup_a.keys import doc_key, parse_key
from leaklab.setup_a.provision import MIN_SCHEDULE, index_body, indexer_body
from search_acl_probe import SPEC_PATH, Recorder, Search, endpoint_from_config, excerpt

INDEX_B = "ll-sp-index-b"
INDEXER_B = "ll-sp-indexer-b"
SELECT = "id,metadata_spo_item_name,metadata_spo_item_path,content,UserIds,GroupIds"
app = typer.Typer(add_completion=False, no_args_is_help=True)


def index_b_variants() -> list[tuple[str, dict[str, Any]]]:
    """Bodies to try, closest to ll-sp-index first."""
    same = copy.deepcopy(index_body())
    same["name"] = INDEX_B
    same["permissionFilterOption"] = "disabled"  # UNVERIFIED value; the service answers either way
    plain = copy.deepcopy(same)
    for field in plain["fields"]:
        field.pop("permissionFilter", None)  # a plain collection instead of a permission field
    return [
        ("only permissionFilterOption changed", same),
        ("permissionFilter attributes removed too", plain),
    ]


def indexer_b_body() -> dict[str, Any]:
    body = copy.deepcopy(indexer_body(MIN_SCHEDULE))
    body["name"] = INDEXER_B
    body["targetIndexName"] = INDEX_B
    return body


def wait_for_run(search: Search, rec: Recorder, timeout_s: float = 600.0) -> dict[str, Any] | None:
    started = time.time()
    last: dict[str, Any] | None = None
    while time.time() - started < timeout_s:
        status, data = search.call("GET", f"/indexers/{INDEXER_B}/status")
        if status == 200 and isinstance(data, dict):
            last = data.get("lastResult") if isinstance(data.get("lastResult"), dict) else None
            if last and last.get("status") not in (None, "inProgress"):
                summary = {
                    k: v
                    for k, v in last.items()
                    if k != "finalTrackingState" and k != "initialTrackingState"
                }
                rec.step("indexer_b_run_finished", http=status, result=excerpt(summary))
                return last
        time.sleep(10)
    rec.step("indexer_b_run_timeout", result=excerpt(last))
    return last


@app.command("index-b-create")
def index_b_create() -> None:
    rec = Recorder("index_b_create")
    search = Search(endpoint_from_config())
    try:
        created = None
        for label, body in index_b_variants():
            status, data = search.call("PUT", f"/indexes/{INDEX_B}", body)
            rec.step("create_index_b", variant=label, http=status, result=excerpt(data))
            if status in (200, 201, 204):
                created = label
                break
        if created is None:
            rec.step("blocked", note="the service refused both index bodies; see the steps above")
            return
        rec.data["index_b_difference_from_index_a"] = created
        status, data = search.call("PUT", f"/indexers/{INDEXER_B}", indexer_b_body())
        rec.step("create_indexer_b", http=status, result=excerpt(data))
        if status not in (200, 201, 204):
            return
        run = wait_for_run(search, rec)
        status, stats = search.call("GET", f"/indexes/{INDEX_B}/stats")
        rec.step("index_b_stats", http=status, result=stats)
        status, data = search.call(
            "POST",
            f"/indexes/{INDEX_B}/docs/search",
            {"search": "*", "select": SELECT, "top": 1000, "count": True},
        )
        docs = data.get("value", []) if isinstance(data, dict) else []
        rec.step(
            "service_token_query_index_b",
            http=status,
            count=len(docs),
            note=f"@odata.count={data.get('@odata.count') if isinstance(data, dict) else None}",
        )
        decoded = []
        for d in docs:
            try:
                decoded.append(parse_key(str(d["id"]))[2])
            except (ValueError, KeyError):
                decoded.append("<unparseable>")
        rec.step(
            "keys",
            result={
                "decoded_item_ids_sample": decoded[:3],
                "all_parse": "<unparseable>" not in decoded,
                "all_end_with_suffix_5": all(i.endswith("5") for i in decoded),
            },
        )
        rec.step(
            "acl_fields_sample",
            note="shown for comparison only; never used for decisions",
            result=excerpt(
                [
                    {k: d.get(k) for k in ("metadata_spo_item_name", "UserIds", "GroupIds")}
                    for d in docs[:3]
                ],
                2000,
            ),
        )
        rec.data["summary"] = {
            "documents_returned_to_service_token": len(docs),
            "last_run": excerpt(run, 600),
        }
        _ = doc_key  # the derivation is checked in the 'keys' step by decoding, not re-encoding
    finally:
        rec.save()


@app.command("token")
def token_check() -> None:
    """Verify a real persona token against the tenant's published signing keys."""
    rec = Recorder("token")
    try:
        s = get_settings()
        with open_env(SPEC_PATH) as env:
            upn = next(u.upn for u in env.plan.users if u.persona)
        token = get_persona_token(upn, [SEARCH_SCOPE], allow_device_code=False, use_cache=False)
        rec.step("claims", result=describe_token(token))
        header = jwt.get_unverified_header(token)
        rec.step("header", result={k: header.get(k) for k in ("alg", "typ", "kid")})
        tenant = s.tenant_id.strip()
        keys_url = f"https://login.microsoftonline.com/{tenant}/discovery/v2.0/keys"
        resp = httpx.get(keys_url, timeout=30.0)
        rec.step("jwks", http=resp.status_code, count=len(resp.json().get("keys", [])))
        keys = {k["kid"]: k for k in resp.json().get("keys", [])}
        rec.step("kid_published", result=header.get("kid") in keys)
        key = jwt.PyJWK(keys[header["kid"]]).key
        for label, kwargs in (
            ("audience https://search.azure.com", {"audience": "https://search.azure.com"}),
            ("wrong audience", {"audience": "https://example.invalid"}),
        ):
            try:
                claims = jwt.decode(
                    token,
                    key,
                    algorithms=["RS256"],
                    issuer=[
                        f"https://sts.windows.net/{tenant}/",
                        f"https://login.microsoftonline.com/{tenant}/v2.0",
                    ],
                    **kwargs,
                )
                rec.step("decode", variant=label, result={"ok": True, "tid": claims.get("tid")})
            except jwt.PyJWTError as exc:
                rec.step("decode", variant=label, result={"ok": False, "error": type(exc).__name__})
    finally:
        rec.save()


if __name__ == "__main__":
    app()
