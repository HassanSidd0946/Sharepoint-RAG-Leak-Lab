"""Create (or leave alone) Setup A's data source, index and indexer, with the smallest schedule.

The bodies follow Microsoft's SharePoint ACL docs (api-version 2026-08-01-preview) and the results
of probes/search_acl_probe.py (docs/FINDINGS.md):
  - data source type sharepoint, indexerPermissionOptions [userIds, groupIds], a client-secret
    connection string, container "useQuery" + includeFolder=<FULL https URL of the ll-lab folder>
    (the service rejects the docs' example without the scheme);
  - index with UserIds/GroupIds permission fields and permissionFilterOption enabled
    (retrievable=true on them only so an elevated read can show the stored ACLs: development only);
  - indexer with the three field mappings and schedule {"interval": "PT5M"}: the smallest interval
    the service accepts (PT1M, PT2M, PT4M were rejected: "shorter than the shortest supported
    interval 00:05:00"; the docs say 5 minutes to 24 hours).
No SharePoint-group configuration (that is the point of "basic"). The ingestion app's secret goes
only into the data source request body and is never logged or returned.
"""

from collections.abc import Mapping
from typing import Any

import httpx

from leaklab.setup_a.client import API, DATASOURCE, INDEX, INDEXER

MIN_SCHEDULE = "PT5M"


def datasource_body(
    *, site_url: str, app_id: str, secret: str, tenant_id: str, folder_url: str
) -> dict[str, Any]:
    if not folder_url.startswith("https://"):
        raise ValueError("includeFolder needs the full https:// URL of the folder")
    connection = (
        f"SharePointOnlineEndpoint={site_url};ApplicationId={app_id};"
        f"ApplicationSecret={secret};TenantId={tenant_id}"
    )
    return {
        "name": DATASOURCE,
        "type": "sharepoint",
        "indexerPermissionOptions": ["userIds", "groupIds"],
        "credentials": {"connectionString": connection},
        "container": {"name": "useQuery", "query": f"includeFolder={folder_url}"},
    }


def index_body() -> dict[str, Any]:
    def text(name: str, **extra: Any) -> dict[str, Any]:
        return {"name": name, "type": "Edm.String", **extra}

    return {
        "name": INDEX,
        "fields": [
            text("id", key=True, searchable=False),
            text("metadata_spo_item_name", searchable=True, retrievable=True),
            text("metadata_spo_item_path", searchable=False, retrievable=True),
            text("content", searchable=True, retrievable=True),
            {
                "name": "UserIds",
                "type": "Collection(Edm.String)",
                "permissionFilter": "userIds",
                "filterable": True,
                "retrievable": True,
            },
            {
                "name": "GroupIds",
                "type": "Collection(Edm.String)",
                "permissionFilter": "groupIds",
                "filterable": True,
                "retrievable": True,
            },
        ],
        "permissionFilterOption": "enabled",
    }


def indexer_body(schedule: str = MIN_SCHEDULE) -> dict[str, Any]:
    return {
        "name": INDEXER,
        "dataSourceName": DATASOURCE,
        "targetIndexName": INDEX,
        "schedule": {"interval": schedule},
        "parameters": {
            "configuration": {
                "indexedFileNameExtensions": ".txt",
                "dataToExtract": "contentAndMetadata",
                "failOnUnsupportedContentType": False,
            }
        },
        "fieldMappings": [
            {
                "sourceFieldName": "metadata_spo_site_library_item_id",
                "targetFieldName": "id",
                "mappingFunction": {"name": "base64Encode"},
            },
            {"sourceFieldName": "metadata_user_ids", "targetFieldName": "UserIds"},
            {"sourceFieldName": "metadata_group_ids", "targetFieldName": "GroupIds"},
        ],
    }


def ensure_provisioned(
    http: httpx.Client,
    endpoint: str,
    bearer: str,
    datasource: Mapping[str, Any],
    schedule: str = MIN_SCHEDULE,
) -> dict[str, str]:
    """Create what is missing; fix a wrong schedule. Returns what happened to each object.

    Raises RuntimeError on any non-2xx answer (status and body only, never the secret).
    """
    headers = {"Authorization": f"Bearer {bearer}"}
    base = endpoint.rstrip("/")
    result: dict[str, str] = {}

    def get(path: str) -> httpx.Response:
        return http.get(f"{base}{path}", params={"api-version": API}, headers=headers)

    def put(path: str, body: Mapping[str, Any]) -> None:
        resp = http.put(f"{base}{path}", params={"api-version": API}, json=body, headers=headers)
        if resp.status_code >= 300:
            raise RuntimeError(f"PUT {path} -> HTTP {resp.status_code}: {resp.text[:300]}")

    for label, path, body in (
        ("data source", f"/datasources/{DATASOURCE}", datasource),
        ("index", f"/indexes/{INDEX}", index_body()),
    ):
        status = get(path).status_code
        if status == 404:
            if not body:
                raise RuntimeError(f"{label} is missing and no ingestion app secret is configured")
            put(path, body)
            result[label] = "created"
        elif status == 200:
            result[label] = "unchanged"
        else:
            raise RuntimeError(f"GET {path} -> HTTP {status}")
    current = get(f"/indexers/{INDEXER}")
    if current.status_code == 404:
        put(f"/indexers/{INDEXER}", indexer_body(schedule))
        result["indexer"] = "created"
    elif current.status_code == 200:
        have = (current.json().get("schedule") or {}).get("interval")
        if have != schedule:
            put(f"/indexers/{INDEXER}", indexer_body(schedule))
            result["indexer"] = f"updated (schedule {have!r} -> {schedule!r})"
        else:
            result["indexer"] = "unchanged"
    else:
        raise RuntimeError(f"GET /indexers/{INDEXER} -> HTTP {current.status_code}")
    return result
