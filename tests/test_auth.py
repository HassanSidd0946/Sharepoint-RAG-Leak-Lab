"""Tests for auth.py with MSAL fully mocked: no network, no real secrets."""

import base64
import json
import logging
from pathlib import Path
from typing import Any

import pytest

from leaklab import auth
from leaklab.config import Settings

UPN = "ivy@example.onmicrosoft.com"
PASSWORD = "not-a-real-password"
SCOPES = ["https://graph.microsoft.com/.default"]


def fake_jwt(claims: dict[str, Any]) -> str:
    def b64(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

    return f"{b64(b'{}')}.{b64(json.dumps(claims).encode())}.sig-should-never-be-logged"


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "tenant_id": "tenant-guid",
        "driver_client_id": "driver-id",
        "persona_client_id": "persona-id",
        "cert_thumbprint": "ABCDEF",
        "persona_password": PASSWORD,
        "sp_host": "t.sharepoint.com",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


@pytest.fixture(autouse=True)
def reset(monkeypatch: pytest.MonkeyPatch) -> None:
    auth._confidential_app.cache_clear()
    auth._public_app.cache_clear()
    monkeypatch.setattr(auth, "get_settings", lambda: make_settings())


class FakePublicApp:
    def __init__(self) -> None:
        self.accounts: list[dict[str, Any]] = []
        self.silent: dict[str, Any] | None = None
        self.ropc: dict[str, Any] = {}
        self.flow: dict[str, Any] = {"user_code": "ABC", "message": "go to https://x and enter ABC"}
        self.device: dict[str, Any] = {}
        self.calls: list[str] = []

    def get_accounts(self, username: str) -> list[dict[str, Any]]:
        return self.accounts

    def acquire_token_silent(self, scopes: list[str], account: Any) -> dict[str, Any] | None:
        self.calls.append("silent")
        return self.silent

    def acquire_token_by_username_password(
        self, username: str, password: str, scopes: list[str]
    ) -> dict[str, Any]:
        self.calls.append("ropc")
        return self.ropc

    def initiate_device_flow(self, scopes: list[str]) -> dict[str, Any]:
        self.calls.append("initiate")
        return self.flow

    def acquire_token_by_device_flow(self, flow: dict[str, Any]) -> dict[str, Any]:
        self.calls.append("device")
        return self.device


def use_fake(monkeypatch: pytest.MonkeyPatch) -> FakePublicApp:
    fake = FakePublicApp()
    monkeypatch.setattr(auth, "_public_app", lambda: fake)
    return fake


def good_result(upn: str = UPN) -> dict[str, Any]:
    return {
        "access_token": fake_jwt({"tid": "t", "upn": upn, "scp": "User.Read"}),
        "id_token_claims": {"preferred_username": upn},
    }


# -- scopes -----------------------------------------------------------------------------------


def test_sp_scope_built_from_host() -> None:
    assert auth.sp_scope(make_settings()) == "https://t.sharepoint.com/.default"


@pytest.mark.parametrize("host", ["", "  ", "https://t.sharepoint.com", "t.sharepoint.com/sites"])
def test_sp_scope_rejects_bad_host(host: str) -> None:
    with pytest.raises(auth.AuthError):
        auth.sp_scope(make_settings(sp_host=host))


# -- error mapping ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "result",
    [
        {"error": "interaction_required"},
        {"error": "consent_required"},
        {"error": "invalid_grant", "error_codes": [50076]},
        {"error": "invalid_grant", "error_codes": [1, 65001]},
    ],
)
def test_needs_interaction_true(result: dict[str, Any]) -> None:
    assert auth.needs_interaction(result)


@pytest.mark.parametrize(
    "result",
    [
        {"error": "invalid_grant", "error_codes": [50126]},  # wrong password
        {"error": "invalid_grant", "error_codes": [50057]},  # disabled account
        {"error": "unauthorized_client", "error_codes": [7000218]},
        {},
    ],
)
def test_needs_interaction_false(result: dict[str, Any]) -> None:
    assert not auth.needs_interaction(result)


def test_describe_token_has_only_safe_claims() -> None:
    token = fake_jwt({"tid": "t", "name": "Ivy", "upn": UPN})
    assert auth.describe_token(token) == {"tid": "t", "upn": UPN}
    assert auth.describe_token("garbage") == {"note": "token is not a decodable JWT"}


# -- app-only token ---------------------------------------------------------------------------


def test_app_token_requires_default_scope() -> None:
    with pytest.raises(auth.AuthError, match=r"\.default"):
        auth.get_app_token("https://graph.microsoft.com/User.Read")


def test_app_token_missing_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auth, "get_settings", lambda: make_settings(cert_path=""))
    with pytest.raises(auth.AuthError, match="LEAKLAB_CERT_PATH"):
        auth.get_app_token(auth.GRAPH_DEFAULT_SCOPE)


def test_app_token_missing_key_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    missing = str(tmp_path / "nope.pem")
    monkeypatch.setattr(auth, "get_settings", lambda: make_settings(cert_path=missing))
    with pytest.raises(auth.AuthError, match="Cannot read certificate"):
        auth.get_app_token(auth.GRAPH_DEFAULT_SCOPE)


def test_app_token_builds_cert_credential_and_returns_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    key = tmp_path / "k.pem"
    key.write_text("FAKE-PEM", encoding="utf-8")
    monkeypatch.setattr(auth, "get_settings", lambda: make_settings(cert_path=str(key)))
    seen: dict[str, Any] = {}
    token = fake_jwt({"tid": "t", "roles": ["Sites.Selected"]})

    class FakeConfidential:
        def __init__(self, client_id: str, authority: str, client_credential: Any) -> None:
            seen.update(client_id=client_id, authority=authority, cred=client_credential)

        def acquire_token_for_client(self, scopes: list[str]) -> dict[str, Any]:
            seen["scopes"] = scopes
            return {"access_token": token}

    monkeypatch.setattr(auth.msal, "ConfidentialClientApplication", FakeConfidential)
    assert auth.get_app_token(auth.GRAPH_DEFAULT_SCOPE) == token
    assert seen["client_id"] == "driver-id"
    assert seen["authority"] == "https://login.microsoftonline.com/tenant-guid"
    assert seen["cred"] == {"private_key": "FAKE-PEM", "thumbprint": "ABCDEF"}
    assert seen["scopes"] == [auth.GRAPH_DEFAULT_SCOPE]


def test_app_token_error_is_mapped(monkeypatch: pytest.MonkeyPatch) -> None:
    class FailingApp:
        def acquire_token_for_client(self, scopes: list[str]) -> dict[str, Any]:
            return {"error": "invalid_client", "error_description": "bad cert\ntrace id"}

    monkeypatch.setattr(auth, "_confidential_app", lambda: FailingApp())
    with pytest.raises(auth.AuthError, match="invalid_client.*bad cert"):
        auth.get_app_token(auth.GRAPH_DEFAULT_SCOPE)


# -- persona token ----------------------------------------------------------------------------


def test_persona_password_flow(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = use_fake(monkeypatch)
    fake.ropc = good_result()
    assert auth.get_persona_token(UPN, SCOPES) == fake.ropc["access_token"]
    assert fake.calls == ["ropc"]


def test_persona_uses_cache_first(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = use_fake(monkeypatch)
    fake.accounts = [{"username": UPN}]
    fake.silent = good_result()
    assert auth.get_persona_token(UPN, SCOPES) == fake.silent["access_token"]
    assert fake.calls == ["silent"]


def test_persona_falls_back_to_device_code(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = use_fake(monkeypatch)
    fake.ropc = {"error": "invalid_grant", "error_codes": [50076], "error_description": "MFA"}
    fake.device = good_result()
    assert auth.get_persona_token(UPN, SCOPES) == fake.device["access_token"]
    assert fake.calls == ["ropc", "initiate", "device"]


def test_persona_wrong_password_does_not_fall_back(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = use_fake(monkeypatch)
    fake.ropc = {"error": "invalid_grant", "error_codes": [50126], "error_description": "bad pw"}
    with pytest.raises(auth.AuthError, match="50126"):
        auth.get_persona_token(UPN, SCOPES)
    assert fake.calls == ["ropc"]


def test_persona_without_password_uses_device_code(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auth, "get_settings", lambda: make_settings(persona_password=""))
    fake = use_fake(monkeypatch)
    fake.device = good_result()
    auth.get_persona_token(UPN, SCOPES)
    assert fake.calls == ["initiate", "device"]


def test_device_code_rejects_wrong_user(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = use_fake(monkeypatch)
    fake.ropc = {"error": "interaction_required"}
    fake.device = good_result("someone.else@example.onmicrosoft.com")
    with pytest.raises(auth.AuthError, match="not issued to"):
        auth.get_persona_token(UPN, SCOPES)


def test_device_flow_start_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = use_fake(monkeypatch)
    fake.ropc = {"error": "interaction_required"}
    fake.flow = {"error": "invalid_client", "error_description": "no public flows"}
    with pytest.raises(auth.AuthError, match="Could not start device code flow"):
        auth.get_persona_token(UPN, SCOPES)


@pytest.mark.parametrize(("upn", "scopes"), [("", SCOPES), (UPN, [])])
def test_persona_requires_upn_and_scopes(upn: str, scopes: list[str]) -> None:
    with pytest.raises(auth.AuthError):
        auth.get_persona_token(upn, scopes)


def test_tokens_and_passwords_never_logged(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    fake = use_fake(monkeypatch)
    fake.ropc = good_result()
    with caplog.at_level(logging.DEBUG):
        token = auth.get_persona_token(UPN, SCOPES)
    assert token not in caplog.text
    assert "sig-should-never-be-logged" not in caplog.text
    assert PASSWORD not in caplog.text
    assert "acquired" in caplog.text


def test_device_code_disabled_stops_on_interaction_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = use_fake(monkeypatch)
    fake.ropc = {
        "error": "invalid_grant",
        "error_codes": [50076],
        "error_description": "AADSTS50076: MFA needed\nTrace ID: x",
    }
    with pytest.raises(auth.PersonaSignInError) as exc:
        auth.get_persona_token(UPN, SCOPES, allow_device_code=False)
    assert fake.calls == ["ropc"]  # no device flow
    assert exc.value.error_codes == [50076]
    assert exc.value.interaction_required is True
    assert "MFA needed" in exc.value.description
    assert exc.value.upn == UPN


def test_persona_sign_in_error_for_wrong_password_is_structured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = use_fake(monkeypatch)
    fake.ropc = {"error": "invalid_grant", "error_codes": [50126], "error_description": "bad pw"}
    with pytest.raises(auth.PersonaSignInError) as exc:
        auth.get_persona_token(UPN, SCOPES, allow_device_code=False)
    assert exc.value.error == "invalid_grant"
    assert exc.value.error_codes == [50126]
    assert exc.value.interaction_required is False


def test_device_code_disabled_without_password(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auth, "get_settings", lambda: make_settings(persona_password=""))
    fake = use_fake(monkeypatch)
    with pytest.raises(auth.AuthError, match="device code is disabled"):
        auth.get_persona_token(UPN, SCOPES, allow_device_code=False)
    assert fake.calls == []


def test_use_password_false_goes_straight_to_device_code(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = use_fake(monkeypatch)
    fake.device = good_result()
    token = auth.get_persona_token(UPN, SCOPES, use_password=False)
    assert token == fake.device["access_token"]
    assert fake.calls == ["initiate", "device"]  # the password flow was never tried


def test_use_password_false_without_device_code_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = use_fake(monkeypatch)
    with pytest.raises(auth.AuthError, match="device code is disabled"):
        auth.get_persona_token(UPN, SCOPES, use_password=False, allow_device_code=False)
    assert fake.calls == []
