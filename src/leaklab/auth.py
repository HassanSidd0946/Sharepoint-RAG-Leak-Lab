"""MSAL authentication: app-only (certificate) tokens and persona (delegated) tokens.

Inputs: LEAKLAB_* settings (tenant, client IDs, certificate path/thumbprint, persona password).
Outputs: raw access-token strings. Callers must never print or log them; use
`describe_token()` (non-secret claims only) for anything human-readable.

Tokens are cached only in MSAL's in-memory cache, one cache per process. Nothing is written
to disk. Every failure raises AuthError (fail closed); we never return a partial result.
"""

import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

import msal
from rich.console import Console

from leaklab.config import Settings, get_settings, redact_jwt

log = logging.getLogger(__name__)

AUTHORITY_BASE = "https://login.microsoftonline.com"
GRAPH_DEFAULT_SCOPE = "https://graph.microsoft.com/.default"

# UNVERIFIED: which AADSTS codes the username/password (ROPC) flow actually returns in this
# tenant when MFA / consent / Conditional Access blocks it. These are the documented
# "user must interact" codes; confirm with a probe and record in docs/FINDINGS.md.
#   50076 MFA required, 50079 MFA enrollment required, 50158 external challenge,
#   65001 consent required.
INTERACTION_ERROR_CODES: frozenset[int] = frozenset({50076, 50079, 50158, 65001})
INTERACTION_ERROR_NAMES: frozenset[str] = frozenset({"interaction_required", "consent_required"})

_console = Console(stderr=True)


class AuthError(RuntimeError):
    """Token acquisition failed or was refused. Never contains a token or password."""


class PersonaSignInError(AuthError):
    """Username/password sign-in was refused. Carries the exact error for probes to report.

    `error_codes` are the AADSTS numbers; `description` is the full (secret-free) text from
    Entra; `interaction_required` is True for MFA / consent style errors.
    """

    def __init__(self, upn: str, result: dict[str, Any]) -> None:
        super().__init__(f"Username/password sign-in failed for {upn}: {_describe_error(result)}")
        self.upn = upn
        self.error = str(result.get("error", "unknown_error"))
        self.error_codes: list[int] = [int(c) for c in result.get("error_codes") or []]
        self.description = str(result.get("error_description", ""))
        self.interaction_required = needs_interaction(result)


def sp_scope(settings: Settings | None = None) -> str:
    """Return the app-only SharePoint scope, "https://{SP_HOST}/.default"."""
    host = (settings or get_settings()).sp_host.strip()
    if not host or "/" in host:
        raise AuthError("LEAKLAB_SP_HOST must be a bare host name such as tenant.sharepoint.com.")
    return f"https://{host}/.default"


def describe_token(token: str) -> dict[str, Any]:
    """Return only non-secret claims of a token (safe to print or save)."""
    try:
        return redact_jwt(token)
    except ValueError:
        return {"note": "token is not a decodable JWT"}


def _require(settings: Settings, *names: str) -> None:
    missing = [f"LEAKLAB_{n.upper()}" for n in names if not str(getattr(settings, n)).strip()]
    if missing:
        raise AuthError("Missing required setting(s): " + ", ".join(missing))


def _describe_error(result: dict[str, Any]) -> str:
    """One-line, secret-free summary of an MSAL error result."""
    desc = str(result.get("error_description", "")).splitlines()
    return f"{result.get('error', 'unknown_error')} {result.get('error_codes', [])}: " + (
        desc[0] if desc else "no description"
    )


def needs_interaction(result: dict[str, Any]) -> bool:
    """True if an MSAL error result means "a human must complete a sign-in step".

    A wrong password or a disabled account is NOT interaction-required, so we do not fall
    back to device code for those; we fail instead.
    """
    if result.get("error") in INTERACTION_ERROR_NAMES:
        return True
    codes = result.get("error_codes") or []
    return any(code in INTERACTION_ERROR_CODES for code in codes)


def _access_token(result: dict[str, Any], what: str) -> str:
    token = result.get("access_token")
    if isinstance(token, str) and token:
        log.info("acquired %s token: %s", what, describe_token(token))
        return token
    raise AuthError(f"Could not acquire {what} token: {_describe_error(result)}")


@lru_cache(maxsize=1)
def _confidential_app() -> msal.ConfidentialClientApplication:
    """Driver app with certificate credentials. Built once; MSAL keeps its token cache."""
    s = get_settings()
    _require(s, "tenant_id", "driver_client_id", "cert_path", "cert_thumbprint")
    try:
        private_key = Path(s.cert_path).read_text(encoding="utf-8")
    except OSError as exc:
        raise AuthError(f"Cannot read certificate private key at LEAKLAB_CERT_PATH: {exc}") from exc
    return msal.ConfidentialClientApplication(
        s.driver_client_id,
        authority=f"{AUTHORITY_BASE}/{s.tenant_id}",
        # SHA-1 hex thumbprint verified with our certificate on 2026-10-15.
        client_credential={"private_key": private_key, "thumbprint": s.cert_thumbprint},
    )


@lru_cache(maxsize=1)
def _public_app() -> msal.PublicClientApplication:
    """Persona app (public client). Built once; MSAL keeps an in-memory token cache."""
    s = get_settings()
    _require(s, "tenant_id", "persona_client_id")
    return msal.PublicClientApplication(
        s.persona_client_id, authority=f"{AUTHORITY_BASE}/{s.tenant_id}"
    )


def get_app_token(scope: str) -> str:
    """Get an app-only access token for the driver app (client credentials + certificate).

    `scope` must be a ".default" scope, e.g. GRAPH_DEFAULT_SCOPE or sp_scope().
    """
    if not scope.endswith("/.default"):
        raise AuthError(f"App-only tokens need a '.default' scope, got {scope!r}.")
    result: dict[str, Any] = _confidential_app().acquire_token_for_client(scopes=[scope])
    return _access_token(result, "app-only")


def get_persona_token(
    upn: str,
    scopes: list[str],
    *,
    allow_device_code: bool = True,
    use_password: bool = True,
    use_cache: bool = True,
) -> str:
    """Get a delegated token for persona `upn`.

    Order: cached token -> username/password -> device code (only if a human step is needed
    and `allow_device_code` is True). With `allow_device_code=False`, any sign-in error,
    including MFA or consent, raises PersonaSignInError instead of starting device code.
    With `use_password=False` the shared persona password is never used and the sign-in goes
    straight to device code (for accounts, like the admin, whose password is not in .env).
    With `use_cache=False` MSAL's cached token is ignored, so the call always gets a NEW
    token from Entra (needed to compare an old token with a fresh one).
    The returned token is verified to belong to `upn`; a mismatch raises AuthError.
    """
    if not upn.strip() or not scopes:
        raise AuthError("get_persona_token needs a UPN and at least one scope.")
    app = _public_app()

    accounts = app.get_accounts(username=upn) if use_cache else []
    if accounts:
        silent: dict[str, Any] | None = app.acquire_token_silent(scopes, account=accounts[0])
        if silent and silent.get("access_token"):
            return _access_token(silent, f"persona {upn}")

    password = get_settings().persona_password.get_secret_value() if use_password else ""
    if password:
        # Username/password (ROPC) is used ONLY because this is a throwaway test tenant with
        # synthetic users and MFA disabled for them. It is insecure (the app handles the
        # password directly, no MFA) and must NEVER be used against a real tenant.
        result: dict[str, Any] = app.acquire_token_by_username_password(
            upn, password, scopes=scopes
        )
        if result.get("access_token"):
            return _verified(result, upn)
        if not allow_device_code or not needs_interaction(result):
            raise PersonaSignInError(upn, result)
        log.warning("interaction required for %s; falling back to device code", upn)
    elif not allow_device_code:
        raise AuthError("LEAKLAB_PERSONA_PASSWORD is not set and device code is disabled.")
    elif use_password:
        log.warning("LEAKLAB_PERSONA_PASSWORD not set; using device code for %s", upn)

    return _verified(_device_code(app, scopes, upn), upn)


def _device_code(app: msal.PublicClientApplication, scopes: list[str], upn: str) -> dict[str, Any]:
    flow: dict[str, Any] = app.initiate_device_flow(scopes=scopes)
    if "user_code" not in flow:
        raise AuthError(f"Could not start device code flow: {_describe_error(flow)}")
    _console.print(f"[bold]Sign in as {upn}[/bold]: {flow['message']}")
    result: dict[str, Any] = app.acquire_token_by_device_flow(flow)
    return result


def _verified(result: dict[str, Any], upn: str) -> str:
    """Return the token only if it was issued to `upn` (device code lets anyone sign in)."""
    token = _access_token(result, f"persona {upn}")
    names = {
        str(v).lower()
        for v in (
            result.get("id_token_claims", {}).get("preferred_username"),
            describe_token(token).get("upn"),
        )
        if v
    }
    if upn.lower() not in names:
        # The `upn` claim is present in persona access tokens (verified 2026-10-15).
        # UNVERIFIED: that id_token_claims.preferred_username is present (never needed so far).
        raise AuthError(f"Token was not issued to {upn} (got {sorted(names) or 'no identity'}).")
    return token
