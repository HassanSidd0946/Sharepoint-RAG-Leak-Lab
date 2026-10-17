"""Setup 0: plain SharePoint, no search or RAG. The baseline every other setup is compared with.

A read is exactly what probes/consistency_probe.py verified: Graph `GET /drives/{drive}/items/{id}/
content` with the PERSONA's delegated token. Graph answers 302 to a pre-authenticated SharePoint
URL (followed WITHOUT the Authorization header, only on *.sharepoint.com) or 403/404.

  served True   final status 200 and the canary string is in the body (exact match)
  served False  403 or 404 from Graph or from the download, or a 200 body without the canary
  served None   anything else (transport error, 401, 429, 5xx, token problem, unknown redirect
                host): an error, never a denial. A 401 also drops the cached token.

Tokens: one per persona, from auth.get_persona_token (username/password on the test tenant), renewed
when less than `margin_s` seconds of life are left. After a failed sign-in the persona is not
retried for `backoff_s`, so a broken sign-in cannot hammer Entra once per read.

Inputs: persona ids -> UPNs, canary -> drive item id (resolved by the caller), and a token source.
"""

import threading
import time
from collections.abc import Callable, Mapping, Sequence
from urllib.parse import urlsplit

import httpx

from leaklab.auth import describe_token, get_persona_token
from leaklab.runner.model import Observation, RunnerError

GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"
READ_SCOPES = ["https://graph.microsoft.com/Files.Read.All"]
REDIRECT_STATUSES = (301, 302, 303, 307, 308)
DENIED_STATUSES = (403, 404)


def default_token_source(upn: str) -> str:
    """A delegated Graph token for `upn`. Never starts a device-code flow (no human is present)."""
    return get_persona_token(upn, READ_SCOPES, allow_device_code=False)


class TokenCache:
    """Persona tokens with renewal before expiry and a back-off after a failed sign-in."""

    def __init__(
        self,
        source: Callable[[str], str],
        clock: Callable[[], float] = time.time,
        margin_s: float = 300.0,
        backoff_s: float = 30.0,
        lifetime_if_unknown_s: float = 600.0,
    ) -> None:
        self._source = source
        self._clock = clock
        self._margin_s = margin_s
        self._backoff_s = backoff_s
        self._unknown_s = lifetime_if_unknown_s
        self._tokens: dict[str, tuple[str, float]] = {}
        self._failed: dict[str, tuple[float, str]] = {}
        self._lock = threading.Lock()
        self.fetches = 0

    def get(self, upn: str) -> str:
        with self._lock:
            now = self._clock()
            cached = self._tokens.get(upn)
            if cached is not None and cached[1] - self._margin_s > now:
                return cached[0]
            failure = self._failed.get(upn)
            if failure is not None and now - failure[0] < self._backoff_s:
                raise RunnerError(f"sign-in failed {now - failure[0]:.0f}s ago: {failure[1]}")
            try:
                token = self._source(upn)
            except Exception as exc:
                self._failed[upn] = (now, f"{type(exc).__name__}")
                raise
            self.fetches += 1
            self._failed.pop(upn, None)
            exp = describe_token(token).get("exp")
            expires = float(exp) if isinstance(exp, int | float) else now + self._unknown_s
            self._tokens[upn] = (token, expires)
            return token

    def invalidate(self, upn: str) -> None:
        with self._lock:
            self._tokens.pop(upn, None)


class Setup0SharePoint:
    name = "setup0"

    def __init__(
        self,
        *,
        drive_id: str,
        item_ids: Mapping[str, str],
        upns: Mapping[str, str],
        tokens: TokenCache | None = None,
        clock: Callable[[], float] = time.time,
        client: httpx.Client | None = None,
        timeout_s: float = 30.0,
    ) -> None:
        self._drive_id = drive_id
        self._item_ids = dict(item_ids)
        self._upns = dict(upns)
        self._clock = clock
        self._tokens = tokens or TokenCache(default_token_source, clock)
        self._client = client or httpx.Client(timeout=timeout_s)
        self._owns_client = client is None

    # -- SystemUnderTest ---------------------------------------------------------------------

    def prepare(self, personas: Sequence[str], canaries: Sequence[str]) -> None:
        """Sign every persona in now (so a bad password fails before any mutation) and check that
        every canary has a known drive item."""
        for persona in personas:
            self._tokens.get(self._upn(persona))
        missing = [c for c in canaries if c not in self._item_ids]
        if missing:
            raise RunnerError(f"no drive item is known for canaries {missing}")

    def read(self, persona: str, canary: str) -> Observation:
        started = self._clock()

        def obs(served: bool | None, **kw: int | str | None) -> Observation:
            return Observation(
                system=self.name,
                persona=persona,
                canary=canary,
                ts=started,
                served=served,
                latency_ms=round((self._clock() - started) * 1000.0, 1),
                **kw,
            )

        try:
            upn = self._upn(persona)
            token = self._tokens.get(upn)
        except Exception as exc:
            return obs(None, error=f"token:{type(exc).__name__}")
        item = self._item_ids.get(canary)
        if item is None:
            return obs(None, error="unknown canary")
        url = f"{GRAPH_BASE_URL}/drives/{self._drive_id}/items/{item}/content"
        try:
            first = self._client.get(url, headers={"Authorization": f"Bearer {token}"})
            status = first.status_code
            if status in DENIED_STATUSES:
                return obs(False, status=status)
            if status == 401:
                self._tokens.invalidate(upn)
                return obs(None, status=status, error="http:401")
            body = first
            if status in REDIRECT_STATUSES:
                host = urlsplit(first.headers.get("Location", "")).hostname or ""
                if not host.endswith(".sharepoint.com"):
                    return obs(None, status=status, error=f"redirect to unexpected host {host!r}")
                body = self._client.get(first.headers["Location"])  # pre-authenticated, no token
            final = body.status_code
            if final in DENIED_STATUSES:
                return obs(False, status=status, final_status=final)
            if not body.is_success:
                return obs(None, status=status, final_status=final, error=f"http:{final}")
            return obs(canary in body.text, status=status, final_status=final)
        except httpx.TransportError as exc:
            return obs(None, error=f"transport:{type(exc).__name__}")
        except httpx.HTTPError as exc:
            return obs(None, error=f"http-client:{type(exc).__name__}")

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def _upn(self, persona: str) -> str:
        try:
            return self._upns[persona]
        except KeyError:
            raise RunnerError(f"no UPN known for persona {persona!r}") from None
