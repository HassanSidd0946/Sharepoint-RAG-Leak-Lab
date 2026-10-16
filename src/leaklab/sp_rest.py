"""Thin SharePoint REST client (httpx) for the site-level permission operations we need.

Inputs: the site URL (e.g. https://tenant.sharepoint.com/sites/LeakLab) and a token provider
(default: the driver app's app-only SharePoint token, scope https://{SP_HOST}/.default).
Outputs: raw httpx.Response objects (so probes can record status codes); SPRestError on any
non-success response, carrying the parsed error body.

Retries: same policy as GraphClient (see http_retry.py).
Security: requests only go to "<site_url>/_api/..."; the bearer token never leaves that host.

Behavior we have not verified against the real tenant is marked `UNVERIFIED`. Remove the mark
only after a probe confirms it and the result is in docs/FINDINGS.md. Verified on 2026-10-15
(probes/sp_probe.py): everything below except the points still marked UNVERIFIED.
"""

import json
import logging
import random
import time
from collections.abc import Callable
from types import TracebackType
from typing import Any, Literal, Self
from urllib.parse import quote, urlsplit

import httpx

from leaklab.auth import get_app_token, sp_scope
from leaklab.http_retry import MAX_ATTEMPTS, RETRY_STATUSES, retry_delay

log = logging.getLogger(__name__)

ACCEPT = "application/json;odata=nometadata"
JSON_CONTENT_TYPE = "application/json;odata=nometadata"
GroupKind = Literal["owner", "member", "visitor"]


class SPRestError(RuntimeError):
    """A SharePoint REST call failed. `body` is the parsed JSON error (or truncated text)."""

    def __init__(self, status_code: int, path: str, body: Any) -> None:
        super().__init__(f"SharePoint REST HTTP {status_code} for {path}")
        self.status_code = status_code
        self.path = path
        self.body = body


def parse_body(resp: httpx.Response) -> Any:
    """Parsed JSON of a response, or None if it has no body."""
    if not resp.content:
        return None
    return resp.json()


def _literal(value: str) -> str:
    """Encode a server-relative URL for use inside ('...') in a REST path."""
    return quote(value.replace("'", "''"), safe="/")


def folder_item_path(server_relative_url: str) -> str:
    """REST path of the list item behind a folder (where role assignments live)."""
    return f"/web/GetFolderByServerRelativeUrl('{_literal(server_relative_url)}')/ListItemAllFields"


def file_item_path(server_relative_url: str) -> str:
    """REST path of the list item behind a file."""
    return f"/web/GetFileByServerRelativeUrl('{_literal(server_relative_url)}')/ListItemAllFields"


def _default_token() -> str:
    return get_app_token(sp_scope())


class SharePointRest:
    """Minimal SharePoint REST client. Use as a context manager."""

    def __init__(
        self,
        site_url: str,
        token_provider: Callable[[], str] = _default_token,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
        max_attempts: int = MAX_ATTEMPTS,
    ) -> None:
        parts = urlsplit(site_url)
        if parts.scheme != "https" or not parts.hostname:
            raise ValueError(f"site_url must be an https URL, got {site_url!r}")
        self._api = site_url.rstrip("/") + "/_api"
        self._token_provider = token_provider
        self._sleep = sleep
        self._rng = rng or random.Random()
        self._max_attempts = max_attempts
        self._http = httpx.Client(transport=transport, timeout=30.0)

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    # -- core ------------------------------------------------------------------------------

    @staticmethod
    def _error(resp: httpx.Response, path: str) -> SPRestError:
        try:
            body: Any = resp.json()
        except ValueError:
            body = resp.text[:2000]
        return SPRestError(resp.status_code, path, body)

    def request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
    ) -> httpx.Response:
        """Send a request to "<site_url>/_api" + `path` (path starts with "/").

        Returns the 2xx response. Raises SPRestError otherwise (after retries for 429/503/504).
        """
        if not path.startswith("/"):
            raise ValueError("path must start with '/'")
        url = self._api + path
        content = json.dumps(body) if body is not None else None
        for attempt in range(1, self._max_attempts + 1):
            headers = {"Authorization": f"Bearer {self._token_provider()}", "Accept": ACCEPT}
            if content is not None:
                headers["Content-Type"] = JSON_CONTENT_TYPE
            resp = self._http.request(method, url, content=content, params=params, headers=headers)
            if resp.is_success:
                return resp
            if resp.status_code in RETRY_STATUSES and attempt < self._max_attempts:
                delay = retry_delay(resp, attempt, self._rng)
                log.warning(
                    "SP REST %s %s -> %s; retry %d/%d in %.1fs",
                    method,
                    path,
                    resp.status_code,
                    attempt,
                    self._max_attempts - 1,
                    delay,
                )
                self._sleep(delay)
                continue
            raise self._error(resp, path)
        raise AssertionError("unreachable")  # pragma: no cover

    # -- permission operations ---------------------------------------------------------------

    def get_unique_flag(self, item_path: str) -> httpx.Response:
        """GET <item>?$select=HasUniqueRoleAssignments -> {"HasUniqueRoleAssignments": bool}."""
        return self.request("GET", item_path, params={"$select": "HasUniqueRoleAssignments"})

    def break_role_inheritance(
        self, item_path: str, *, copy_role_assignments: bool, clear_subscopes: bool
    ) -> httpx.Response:
        """POST <item>/breakroleinheritance(...).

        Works with the app-only token and no X-RequestDigest; returns 200 with
        {"odata.null": true}. Verified on a folder's and on a file's list item (2026-10-15).
        UNVERIFIED: what clear_subscopes does to children that have unique permissions.
        """
        copy = str(copy_role_assignments).lower()
        clear = str(clear_subscopes).lower()
        return self.request(
            "POST",
            f"{item_path}/breakroleinheritance(copyRoleAssignments={copy},clearSubscopes={clear})",
        )

    def ensure_user(self, logon_name: str) -> httpx.Response:
        """POST /web/ensureuser {"logonName": ...}: resolve a claim to a SharePoint principal.

        Verified (2026-10-15): an Entra security group via "c:0t.c|tenant|<group-object-id>"
        (PrincipalType=4), and a user via its plain UPN, which returned the LoginName
        "i:0#.f|membership|<upn lowercase>" (PrincipalType=1). A brand-new group can be
        answered with HTTP 400 "The specified user ... could not be found" for a variable time
        (seen 3 times); callers must retry that error.
        """
        return self.request("POST", "/web/ensureuser", body={"logonName": logon_name})

    def get_role_definitions(self) -> httpx.Response:
        """GET /web/roledefinitions. Look roles up by RoleTypeKind/Name; never hardcode the ids."""
        return self.request(
            "GET", "/web/roledefinitions", params={"$select": "Id,Name,RoleTypeKind"}
        )

    def add_role_assignment(
        self, item_path: str, principal_id: int, role_definition_id: int
    ) -> httpx.Response:
        """POST <item>/roleassignments/addroleassignment(principalid=..,roledefid=..).

        Verified after inheritance was broken, on a folder's and on a file's list item, and on
        /web (site root): 200, {"odata.null": true} (2026-10-15).
        """
        return self.request(
            "POST",
            f"{item_path}/roleassignments/addroleassignment"
            f"(principalid={principal_id},roledefid={role_definition_id})",
        )

    def remove_role_assignment(
        self, item_path: str, principal_id: int, role_definition_id: int
    ) -> httpx.Response:
        """POST <item>/roleassignments/removeroleassignment(principalid=..,roledefid=..).

        Verified on /web (site root) 2026-10-15: 200, and the assignment was gone on re-read
        (used by sp_probe's root-grant --cleanup).
        """
        return self.request(
            "POST",
            f"{item_path}/roleassignments/removeroleassignment"
            f"(principalid={principal_id},roledefid={role_definition_id})",
        )

    def get_user_effective_permissions(self, item_path: str, logon_name: str) -> httpx.Response:
        """GET <item>/getusereffectivepermissions(@u)?@u='<logon name>': SharePoint's own opinion.

        Returns the user's BasePermissions as {"Low": "<uint32>", "High": "<uint32>"} (possibly
        wrapped in "value"). UNVERIFIED: that the endpoint exists on a list item, accepts the
        app-only token, and the exact response shape.
        """
        escaped = logon_name.replace("'", "''")
        return self.request(
            "GET", f"{item_path}/getusereffectivepermissions(@u)", params={"@u": f"'{escaped}'"}
        )

    def get_role_assignments(self, item_path: str) -> httpx.Response:
        """GET <item>/roleassignments?$expand=Member,RoleDefinitionBindings.

        Returns {"value": [{"Member": {...}, "RoleDefinitionBindings": [...], "PrincipalId"}]}.
        An item that inherits shows its parent's assignments; use get_unique_flag to tell.
        """
        return self.request(
            "GET",
            f"{item_path}/roleassignments",
            params={"$expand": "Member,RoleDefinitionBindings"},
        )

    def get_associated_group(self, kind: GroupKind) -> httpx.Response:
        """GET /web/associated{owner|member|visitor}group -> {"Id", "Title", "LoginName"}."""
        return self.request(
            "GET", f"/web/associated{kind}group", params={"$select": "Id,Title,LoginName"}
        )

    def get_group_users(self, group_id: int) -> httpx.Response:
        """GET /web/sitegroups/getbyid(<id>)/users (verified for a user member).

        UNVERIFIED: how an Entra group that is a member of a SharePoint group appears.
        """
        return self.request(
            "GET",
            f"/web/sitegroups/getbyid({group_id})/users",
            params={"$select": "Id,Title,LoginName,PrincipalType"},
        )
