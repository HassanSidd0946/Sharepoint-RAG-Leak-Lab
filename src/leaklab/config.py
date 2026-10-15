"""Settings from environment/.env, the tenant safety guard, and JWT claim redaction.

Inputs: LEAKLAB_* environment variables or a local .env file (see .env.example).
Outputs: a cached Settings object; assert_test_tenant() which aborts on the wrong tenant;
redact_jwt() which reduces a token to its non-secret claims so it is safe to log or save.
"""

import base64
import binascii
import json
from functools import lru_cache
from typing import Any

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# Claims that are safe to keep when saving probe output. Everything else is dropped.
SAFE_CLAIMS: tuple[str, ...] = ("tid", "aud", "scp", "roles", "oid", "upn", "appid", "iss", "exp")


class Settings(BaseSettings):
    """All LEAKLAB_* configuration. Empty string means "not set"."""

    model_config = SettingsConfigDict(
        env_prefix="LEAKLAB_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    tenant_id: str = ""
    allowed_tenant_id: str = ""
    driver_client_id: str = ""
    cert_path: str = ""
    cert_public_path: str = ""
    cert_thumbprint: str = ""
    persona_client_id: str = ""
    persona_password: SecretStr = SecretStr("")
    site_url: str = ""
    sp_host: str = ""
    admin_upn: str = ""  # UPN of your own admin account (delegated reads via device code)
    usage_location: str = ""  # two-letter country code, needed before assigning licenses


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide Settings (read once, then cached)."""
    return Settings()


def assert_test_tenant(tid: str, settings: Settings | None = None) -> None:
    """Abort unless `tid` is the one tenant we are allowed to write to.

    Every script that writes to a tenant must call this first, with the tenant ID taken
    from the acquired token (not from our own config). Fails closed: if the allowed tenant
    is not configured, or `tid` is empty, it raises.
    """
    allowed = (settings or get_settings()).allowed_tenant_id.strip().lower()
    actual = tid.strip().lower()
    if not allowed:
        raise RuntimeError(
            "Refusing to continue: LEAKLAB_ALLOWED_TENANT_ID is not set. "
            "Set it to the test tenant's ID in .env."
        )
    if not actual:
        raise RuntimeError("Refusing to continue: the token has no tenant ID (tid).")
    if actual != allowed:
        raise RuntimeError(
            f"Refusing to continue: token tenant {tid!r} does not match "
            f"LEAKLAB_ALLOWED_TENANT_ID {allowed!r}. This is not the test tenant."
        )


# Keys whose values are removed from any payload we print or save (matched case-insensitively).
# Pre-authenticated download URLs and credential-like fields can grant access if leaked.
REDACT_KEYS: frozenset[str] = frozenset(
    {
        "@microsoft.graph.downloadurl",
        "@content.downloadurl",
        "downloadurl",
        "password",
        "access_token",
        "id_token",
        "refresh_token",
        "client_secret",
        "authorization",
    }
)


def redact_payload(obj: Any) -> Any:
    """Return a copy of a JSON-like payload that is safe to print or save to probes/out.

    Removes REDACT_KEYS everywhere, and the `webUrl` of any sharing `link` object (sharing
    links can contain secrets). Everything else (ids, names, roles) is kept: the point of a
    probe is to see the real response shape.
    """
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for key, value in obj.items():
            if str(key).lower() in REDACT_KEYS:
                out[key] = "<redacted>"
            elif key == "link" and isinstance(value, dict):
                out[key] = {
                    k: ("<redacted>" if k == "webUrl" else redact_payload(v))
                    for k, v in value.items()
                }
            else:
                out[key] = redact_payload(value)
        return out
    if isinstance(obj, list):
        return [redact_payload(item) for item in obj]
    return obj


def redact_jwt(token: str) -> dict[str, Any]:
    """Decode a JWT's payload WITHOUT verifying it and return only non-secret claims.

    The signature is never checked: this is for logging/saving probe output only, never for
    trusting a token. Raises ValueError if `token` is not a decodable JWT.
    """
    parts = token.strip().split(".")
    if len(parts) != 3:
        raise ValueError("Not a JWT: expected 3 dot-separated parts.")
    payload_b64 = parts[1]
    try:
        raw = base64.urlsafe_b64decode(payload_b64 + "=" * (-len(payload_b64) % 4))
        claims = json.loads(raw)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("Not a JWT: payload is not valid base64url JSON.") from exc
    if not isinstance(claims, dict):
        raise ValueError("Not a JWT: payload is not a JSON object.")
    return {k: claims[k] for k in SAFE_CLAIMS if k in claims}
