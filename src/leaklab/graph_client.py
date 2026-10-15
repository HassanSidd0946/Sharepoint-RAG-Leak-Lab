"""Thin Microsoft Graph v1.0 client (httpx): retries, paging, and only the calls we need.

Inputs: a token provider (default: the driver app's app-only Graph token from auth.py).
Outputs: parsed JSON dicts; GraphError on any non-success response.

Retries: see http_retry.py (429/503/504, 5 attempts, Retry-After honored, backoff + jitter).
Other statuses and transport errors are NOT retried, so callers that create things should
look first (see graph_probe.py).

Security: the bearer token is only ever sent to graph.microsoft.com. A @odata.nextLink (or
any absolute URL) pointing anywhere else is refused. Request bodies, tokens and passwords
are never included in error messages or logs.
"""

import logging
import random
import time
from collections.abc import Callable
from types import TracebackType
from typing import Any, Self
from urllib.parse import quote, urlsplit

import httpx

from leaklab.auth import GRAPH_DEFAULT_SCOPE, get_app_token
from leaklab.http_retry import MAX_ATTEMPTS, RETRY_STATUSES, retry_delay

log = logging.getLogger(__name__)

GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"
GRAPH_HOST = "graph.microsoft.com"
MAX_PAGES = 1000


class GraphError(RuntimeError):
    """A Graph call failed. `status_code` is 0 when no HTTP request was made."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(f"Graph HTTP {status_code} {code}: {message}")
        self.status_code = status_code
        self.code = code
        self.message = message


def _default_token() -> str:
    return get_app_token(GRAPH_DEFAULT_SCOPE)


class GraphClient:
    """Minimal Graph client. Use as a context manager so the connection pool is closed."""

    def __init__(
        self,
        token_provider: Callable[[], str] = _default_token,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
        max_attempts: int = MAX_ATTEMPTS,
    ) -> None:
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
    def _full_url(url: str) -> str:
        if url.startswith("/"):
            return GRAPH_BASE_URL + url
        parts = urlsplit(url)
        if parts.scheme == "https" and parts.hostname == GRAPH_HOST:
            return url
        raise GraphError(0, "untrusted_url", f"refusing to send credentials to {parts.hostname!r}")

    @staticmethod
    def _error(resp: httpx.Response) -> GraphError:
        try:
            err = resp.json().get("error", {})
            code, message = str(err.get("code", "unknown")), str(err.get("message", ""))
        except (ValueError, AttributeError):
            code, message = "non_json_error", ""
        return GraphError(resp.status_code, code, message)

    def request(
        self,
        method: str,
        url: str,
        *,
        json: dict[str, Any] | None = None,
        content: bytes | str | None = None,
        headers: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
    ) -> httpx.Response:
        """Send a request (relative path like "/users", or an absolute Graph URL).

        `content`/`headers` are for raw bodies such as file uploads (PUT .../content).

        Returns the 2xx response. Raises GraphError otherwise (after retries for 429/503/504).
        """
        full = self._full_url(url)
        for attempt in range(1, self._max_attempts + 1):
            resp = self._http.request(
                method,
                full,
                json=json,
                content=content,
                params=params,
                headers={**(headers or {}), "Authorization": f"Bearer {self._token_provider()}"},
            )
            if resp.is_success:
                return resp
            if resp.status_code in RETRY_STATUSES and attempt < self._max_attempts:
                delay = retry_delay(resp, attempt, self._rng)
                log.warning(
                    "Graph %s %s -> %s; retry %d/%d in %.1fs",
                    method,
                    urlsplit(full).path,
                    resp.status_code,
                    attempt,
                    self._max_attempts - 1,
                    delay,
                )
                self._sleep(delay)
                continue
            raise self._error(resp)
        raise AssertionError("unreachable")  # pragma: no cover

    def get(self, url: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        result: dict[str, Any] = self.request("GET", url, params=params).json()
        return result

    def get_paged(self, url: str, params: dict[str, str] | None = None) -> list[dict[str, Any]]:
        """GET a collection and follow @odata.nextLink until exhausted; return all items."""
        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        next_url: str | None = url
        next_params = params
        while next_url is not None:
            if next_url in seen or len(seen) >= MAX_PAGES:
                raise GraphError(0, "paging_loop", "nextLink repeated or too many pages")
            seen.add(next_url)
            page = self.get(next_url, next_params)
            items.extend(page.get("value", []))
            next_url = page.get("@odata.nextLink")
            next_params = None  # nextLink already carries the query string
        return items

    # -- users -----------------------------------------------------------------------------

    def create_user(
        self, upn: str, display_name: str, mail_nickname: str, password: str
    ) -> dict[str, Any]:
        """POST /users. Does not force a password change. Returns the created user."""
        body: dict[str, Any] = {
            "accountEnabled": True,
            "displayName": display_name,
            "mailNickname": mail_nickname,
            "userPrincipalName": upn,
            "passwordProfile": {"forceChangePasswordNextSignIn": False, "password": password},
        }
        result: dict[str, Any] = self.request("POST", "/users", json=body).json()
        return result

    def get_user_by_upn(self, upn: str) -> dict[str, Any] | None:
        """GET /users/{upn}; None if it does not exist (404)."""
        try:
            return self.get(
                f"/users/{quote(upn, safe='@')}",
                {"$select": "id,userPrincipalName,displayName"},
            )
        except GraphError as exc:
            if exc.status_code == 404:
                return None
            raise

    def delete_user(self, user_id: str) -> None:
        # UNVERIFIED: deleted users go to the soft-delete bin; whether the same UPN can be
        # re-created straight away is not yet checked.
        self.request("DELETE", f"/users/{user_id}")

    # -- groups ----------------------------------------------------------------------------

    def create_security_group(self, display_name: str, mail_nickname: str) -> dict[str, Any]:
        """POST /groups: a plain (non-mail-enabled, non-M365) Entra security group."""
        body: dict[str, Any] = {
            "displayName": display_name,
            "mailNickname": mail_nickname,
            "mailEnabled": False,
            "securityEnabled": True,
            "groupTypes": [],
        }
        result: dict[str, Any] = self.request("POST", "/groups", json=body).json()
        return result

    def find_group_by_name(self, display_name: str) -> dict[str, Any] | None:
        """Look a group up by exact displayName (needed so creation can be idempotent).

        Returns None if there is none; raises GraphError if the name is ambiguous.
        """
        escaped = display_name.replace("'", "''")
        found = self.get_paged(
            "/groups", {"$filter": f"displayName eq '{escaped}'", "$select": "id,displayName"}
        )
        if len(found) > 1:
            raise GraphError(0, "ambiguous_group", f"{len(found)} groups named {display_name!r}")
        return found[0] if found else None

    def delete_group(self, group_id: str) -> None:
        self.request("DELETE", f"/groups/{group_id}")

    def _add_member(self, group_id: str, member_id: str) -> bool:
        """POST /groups/{id}/members/$ref. True if added, False if it was already a member."""
        body = {"@odata.id": f"{GRAPH_BASE_URL}/directoryObjects/{member_id}"}
        try:
            self.request("POST", f"/groups/{group_id}/members/$ref", json=body)
        except GraphError as exc:
            # UNVERIFIED: the exact error Graph returns for an existing member; we assume
            # HTTP 400 with "already exist" in the message.
            if exc.status_code == 400 and "already exist" in exc.message.lower():
                return False
            raise
        return True

    def add_user_to_group(self, group_id: str, user_id: str) -> bool:
        return self._add_member(group_id, user_id)

    def add_group_to_group(self, parent_group_id: str, child_group_id: str) -> bool:
        """Make `child` a member of `parent` (nesting)."""
        return self._add_member(parent_group_id, child_group_id)

    def list_transitive_member_of(self, user_id: str) -> list[dict[str, Any]]:
        """GET /users/{id}/transitiveMemberOf: groups (and roles) the user is in, any depth."""
        return self.get_paged(f"/users/{user_id}/transitiveMemberOf", {"$select": "id,displayName"})
