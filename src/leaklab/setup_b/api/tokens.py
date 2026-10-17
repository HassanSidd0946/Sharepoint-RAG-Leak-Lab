"""Validate the end user's access token: signature, issuer, tenant, audience, expiry, and that it
is a USER token. Every failure is InvalidToken (fail closed); the message never contains the token.

Verified 2026-10-17 (probes/search_index_b_probe.py token, docs/FINDINGS.md): a persona token for
`https://search.azure.com/.default` is an RS256 JWT with `aud` https://search.azure.com, `iss`
https://sts.windows.net/<tid>/, `tid`, `oid`, `upn`, `scp`; the tenant's keys are published at
login.microsoftonline.com/<tid>/discovery/v2.0/keys and PyJWT verifies the signature with them.

There is no API app registration in this tenant, so the accepted audience is a SETTING
(LEAKLAB_API_AUDIENCE, default https://search.azure.com). docs/ARCHITECTURE.md has the consequence.
Keys come from a KeySource: the real one fetches the JWKS (cached; one refetch, at most once a
minute, when a token names an unknown `kid`); tests use their own.
"""

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
import jwt

KEYS_URL = "https://login.microsoftonline.com/{tenant}/discovery/v2.0/keys"
REFETCH_MIN_S = 60.0
ALGORITHMS = ["RS256"]


class InvalidToken(Exception):
    """The token is not acceptable. `code` is a short, safe reason (no token content)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Principal:
    oid: str  # the Entra object id: the key of the membership state
    upn: str  # lower-case


class KeySource(Protocol):
    def key_for(self, kid: str) -> Any | None: ...


class JwksKeys:
    """The tenant's signing keys, fetched over HTTPS and cached."""

    def __init__(
        self,
        tenant_id: str,
        *,
        client: httpx.Client | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._url = KEYS_URL.format(tenant=tenant_id)
        self._http = client or httpx.Client(timeout=15.0)
        self._clock = clock
        self._lock = threading.Lock()
        self._keys: dict[str, Any] = {}
        self._fetched_at = float("-inf")

    def key_for(self, kid: str) -> Any | None:
        with self._lock:
            if kid not in self._keys and self._clock() - self._fetched_at >= REFETCH_MIN_S:
                self._refresh()
            return self._keys.get(kid)

    def _refresh(self) -> None:
        self._fetched_at = self._clock()  # also after a failure: no hammering
        try:
            resp = self._http.get(self._url)
            resp.raise_for_status()
            keys = resp.json().get("keys", [])
            self._keys = {k["kid"]: jwt.PyJWK(k).key for k in keys if k.get("kty") == "RSA"}
        except (httpx.HTTPError, ValueError, KeyError, jwt.PyJWTError):
            pass  # keep what we had; an unknown kid then fails closed


class TokenValidator:
    def __init__(
        self,
        *,
        tenant_id: str,
        audience: str,
        keys: KeySource,
        leeway_s: float = 60.0,
    ) -> None:
        if not tenant_id.strip() or not audience.strip():
            raise ValueError("tenant_id and audience are required")
        self._tenant = tenant_id.strip().lower()
        self._audience = audience.strip()
        self._keys = keys
        self._leeway = leeway_s
        self._issuers = [
            f"https://sts.windows.net/{self._tenant}/",
            f"https://login.microsoftonline.com/{self._tenant}/v2.0",
        ]

    def validate(self, token: str) -> Principal:
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError:
            raise InvalidToken("malformed") from None
        if header.get("alg") not in ALGORITHMS:
            raise InvalidToken("algorithm")  # includes "none" and HS256 downgrade attempts
        kid = header.get("kid")
        key = self._keys.key_for(str(kid)) if kid else None
        if key is None:
            raise InvalidToken("unknown_key")
        try:
            claims = jwt.decode(
                token,
                key,
                algorithms=ALGORITHMS,
                audience=self._audience,
                issuer=self._issuers,
                leeway=self._leeway,
                options={"require": ["exp", "iss", "aud"]},
            )
        except jwt.ExpiredSignatureError:
            raise InvalidToken("expired") from None
        except jwt.InvalidAudienceError:
            raise InvalidToken("audience") from None
        except jwt.InvalidIssuerError:
            raise InvalidToken("issuer") from None
        except jwt.PyJWTError:
            raise InvalidToken("invalid") from None
        if str(claims.get("tid", "")).lower() != self._tenant:
            raise InvalidToken("tenant")
        oid, upn = claims.get("oid"), claims.get("upn") or claims.get("preferred_username")
        if not isinstance(oid, str) or not oid or not isinstance(upn, str) or not upn:
            raise InvalidToken("no_user_identity")
        if not claims.get("scp") or claims.get("idtyp") == "app":
            raise InvalidToken("not_a_user_token")  # an app-only token has no user to trim for
        return Principal(oid=oid.lower(), upn=upn.lower())
