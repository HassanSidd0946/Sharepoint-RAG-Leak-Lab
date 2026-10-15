"""Tests for config.py: tenant guard, settings loading, JWT redaction (fake unsigned JWTs)."""

import base64
import json
from typing import Any

import pytest

from leaklab.config import Settings, assert_test_tenant, redact_jwt, redact_payload

TENANT = "11111111-2222-3333-4444-555555555555"


def settings_with(allowed: str) -> Settings:
    return Settings(_env_file=None, allowed_tenant_id=allowed)  # type: ignore[call-arg]


def fake_jwt(claims: dict[str, Any]) -> str:
    def b64(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

    header = b64(json.dumps({"alg": "none", "typ": "JWT"}).encode())
    return f"{header}.{b64(json.dumps(claims).encode())}."


def test_assert_test_tenant_accepts_match() -> None:
    assert_test_tenant(TENANT, settings_with(TENANT))


def test_assert_test_tenant_ignores_case_and_whitespace() -> None:
    assert_test_tenant(f"  {TENANT.upper()} ", settings_with(TENANT))


def test_assert_test_tenant_rejects_other_tenant() -> None:
    with pytest.raises(RuntimeError, match="does not match"):
        assert_test_tenant("99999999-0000-0000-0000-000000000000", settings_with(TENANT))


def test_assert_test_tenant_fails_closed_when_unconfigured() -> None:
    with pytest.raises(RuntimeError, match="not set"):
        assert_test_tenant(TENANT, settings_with(""))


def test_assert_test_tenant_rejects_empty_tid() -> None:
    with pytest.raises(RuntimeError, match="no tenant ID"):
        assert_test_tenant("", settings_with(TENANT))


def test_settings_read_env_and_hide_password(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEAKLAB_ALLOWED_TENANT_ID", TENANT)
    monkeypatch.setenv("LEAKLAB_PERSONA_PASSWORD", "hunter2")
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.allowed_tenant_id == TENANT
    assert s.persona_password.get_secret_value() == "hunter2"
    assert "hunter2" not in repr(s)


def test_redact_jwt_keeps_only_safe_claims() -> None:
    token = fake_jwt(
        {
            "tid": TENANT,
            "aud": "https://graph.microsoft.com",
            "roles": ["Sites.Selected"],
            "oid": "abc",
            "exp": 1800000000,
            "name": "Ivy Intern",
            "unique_name": "ivy@example.com",
            "wids": ["secret-ish"],
        }
    )
    assert redact_jwt(token) == {
        "tid": TENANT,
        "aud": "https://graph.microsoft.com",
        "roles": ["Sites.Selected"],
        "oid": "abc",
        "exp": 1800000000,
    }


@pytest.mark.parametrize("bad", ["", "abc", "a.b", "a.b.c.d", "a.!!!.c"])
def test_redact_jwt_rejects_malformed(bad: str) -> None:
    with pytest.raises(ValueError):
        redact_jwt(bad)


def test_redact_jwt_rejects_non_object_payload() -> None:
    payload = base64.urlsafe_b64encode(b"[1,2]").rstrip(b"=").decode()
    with pytest.raises(ValueError, match="JSON object"):
        redact_jwt(f"x.{payload}.")


def test_redact_payload_strips_download_urls_links_and_secrets() -> None:
    payload = {
        "id": "1",
        "@microsoft.graph.downloadUrl": "https://x/secret",
        "value": [
            {"roles": ["read"], "link": {"type": "view", "webUrl": "https://x/:t:/s/tok"}},
            {"grantedToV2": {"user": {"displayName": "Ivy"}}},
        ],
        "nested": {"Password": "pw", "keep": 1},
    }
    out = redact_payload(payload)
    assert out["@microsoft.graph.downloadUrl"] == "<redacted>"
    assert out["value"][0]["link"] == {"type": "view", "webUrl": "<redacted>"}
    assert out["value"][0]["roles"] == ["read"]
    assert out["value"][1]["grantedToV2"]["user"]["displayName"] == "Ivy"
    assert out["nested"] == {"Password": "<redacted>", "keep": 1}
    assert payload["nested"]["Password"] == "pw"  # input not mutated


def test_usage_location_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEAKLAB_USAGE_LOCATION", "US")
    assert Settings(_env_file=None).usage_location == "US"  # type: ignore[call-arg]


def test_admin_upn_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEAKLAB_ADMIN_UPN", "admin@example.com")
    assert Settings(_env_file=None).admin_upn == "admin@example.com"  # type: ignore[call-arg]
