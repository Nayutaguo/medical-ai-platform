from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from pydantic import ValidationError

from medical_ai.api import create_app
from medical_ai.authorization import AccessContext, PermissionCode
from medical_ai.config import Settings
from medical_ai.identity.errors import CsrfValidationError, InvalidPasswordResetError
from medical_ai.identity.models import ActiveSession, IssuedPasswordReset
from medical_ai.identity.tokens import hash_opaque_token
from medical_ai.services import ServiceResult


class MinimalAnalyticsService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def liveness(self) -> ServiceResult:
        return ServiceResult(data={"status": "alive"})


class PasswordAuthenticationService:
    session_token = "opaque-session"
    csrf_token = "csrf-proof"

    def __init__(self) -> None:
        now = datetime(2026, 8, 26, 8, 0)
        context = AccessContext(
            user_id="user-1",
            organization_id="organization-1",
            membership_id="membership-1",
            permissions=frozenset({PermissionCode.ANALYTICS_QUERY_EXECUTE}),
            allowed_facility_ids=frozenset(),
            identity_version=1,
            authorization_version=1,
            session_id="session-1",
        )
        self.active = ActiveSession(
            session_id="session-1",
            user_id="user-1",
            email="analyst@example.com",
            display_name="Analyst",
            organization_name="Organization 1",
            expires_at=now + timedelta(hours=1),
            idle_expires_at=now + timedelta(minutes=20),
            csrf_token_hash=hash_opaque_token(self.csrf_token),
            access_context=context,
        )
        self.change_calls: list[tuple] = []
        self.request_calls: list[tuple] = []
        self.reset_calls: list[tuple] = []
        self.reset_failure = False
        self.issue_reset = True

    def resolve_session(self, token: str):
        return self.active if token == self.session_token else None

    def require_csrf(self, _session: ActiveSession, token: str) -> None:
        if token != self.csrf_token:
            raise CsrfValidationError

    def change_password(self, session, current_password, new_password, **kwargs):
        self.change_calls.append(
            (session, current_password, new_password, kwargs["request_id"])
        )

    def request_password_reset(self, email, **kwargs):
        self.request_calls.append((email, kwargs))
        if not self.issue_reset:
            return None
        return IssuedPasswordReset(
            token="one-time-reset-secret",
            expires_at=datetime(2026, 8, 26, 8, 30),
            organization_id="organization-1",
        )

    def reset_password(self, token, email, organization_id, new_password, **kwargs):
        self.reset_calls.append(
            (token, email, organization_id, new_password, kwargs["request_id"])
        )
        if self.reset_failure:
            raise InvalidPasswordResetError


def _client(*, expose_reset_token: bool = False):
    settings = Settings(
        _env_file=None,
        auth_enforcement_enabled=True,
        auth_session_cookie_secure=False,
        auth_session_cookie_name="test_session",
        auth_dev_expose_password_reset_token=expose_reset_token,
    )
    authentication = PasswordAuthenticationService()
    app = create_app(
        settings=settings,
        analytics_service=MinimalAnalyticsService(settings),
        authentication_service=authentication,
    )
    app.config["TESTING"] = True
    return app.test_client(), authentication


def test_password_change_requires_csrf_and_expires_both_cookies() -> None:
    client, authentication = _client()
    client.set_cookie("test_session", authentication.session_token, path="/api/v1")

    denied = client.post(
        "/api/v1/auth/password/change",
        json={
            "current_password": "current password value",
            "new_password": "replacement password value",
        },
    )
    assert denied.status_code == 403
    assert authentication.change_calls == []

    response = client.post(
        "/api/v1/auth/password/change",
        json={
            "current_password": "current password value",
            "new_password": "replacement password value",
        },
        headers={"X-CSRF-Token": authentication.csrf_token},
    )
    assert response.status_code == 200
    assert response.get_json()["data"] == {"changed": True}
    assert len(authentication.change_calls) == 1
    serialized = response.get_data(as_text=True)
    assert "current password value" not in serialized
    assert "replacement password value" not in serialized
    cookies = response.headers.getlist("Set-Cookie")
    assert any("test_session=" in cookie and "Max-Age=0" in cookie for cookie in cookies)
    assert any("medical_ai_csrf=" in cookie and "Max-Age=0" in cookie for cookie in cookies)


def test_reset_request_has_same_accepted_shape_and_dev_token_is_explicit() -> None:
    client, authentication = _client()
    authentication.issue_reset = False

    unknown = client.post(
        "/api/v1/auth/password-reset-requests",
        json={"email": "unknown@example.com", "organization_id": "organization-1"},
    )
    assert unknown.status_code == 202
    assert unknown.get_json()["data"] == {"accepted": True}

    dev_client, dev_authentication = _client(expose_reset_token=True)
    issued = dev_client.post(
        "/api/v1/auth/password-reset-requests",
        json={"email": "analyst@example.com", "organization_id": "organization-1"},
    )
    assert issued.status_code == 202
    assert issued.get_json()["data"] == {
        "accepted": True,
        "reset_token": "one-time-reset-secret",
        "expires_at": "2026-08-26T08:30:00",
        "organization_id": "organization-1",
    }
    assert dev_authentication.request_calls[0][1]["organization_id"] == "organization-1"


def test_reset_completion_matches_frontend_contract_and_stable_error() -> None:
    client, authentication = _client()
    payload = {
        "token": "one-time-reset-secret",
        "email": "analyst@example.com",
        "organization_id": "organization-1",
        "new_password": "replacement password value",
    }

    response = client.post("/api/v1/auth/password-resets", json=payload)
    assert response.status_code == 200
    assert response.get_json()["data"] == {"reset": True}
    assert authentication.reset_calls[0][0:4] == (
        "one-time-reset-secret",
        "analyst@example.com",
        "organization-1",
        "replacement password value",
    )
    assert "one-time-reset-secret" not in response.get_data(as_text=True)

    authentication.reset_failure = True
    invalid = client.post("/api/v1/auth/password-resets", json=payload)
    assert invalid.status_code == 400
    assert invalid.get_json()["error"]["code"] == "PASSWORD_RESET_INVALID"
    assert "one-time-reset-secret" not in invalid.get_data(as_text=True)


def test_production_forbids_development_reset_token_exposure() -> None:
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            app_environment="production",
            auth_enforcement_enabled=True,
            auth_session_cookie_secure=True,
            auth_dev_expose_password_reset_token=True,
            mcp_allow_unscoped_tools=False,
            rate_limit_enabled=True,
            redis_url="rediss://redis.internal:6379/0",
            rate_limit_key_secret="production-test-secret-with-32-bytes",
        )
