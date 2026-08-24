from datetime import datetime, timedelta

from medical_ai.api import create_app
from medical_ai.authorization import AccessContext, PermissionCode
from medical_ai.config import Settings
from medical_ai.identity.errors import InvalidCredentialsError, InvalidInvitationError
from medical_ai.identity.passwords import PasswordPolicyError
from medical_ai.identity.models import ActiveSession, IssuedSession
from medical_ai.identity.tokens import hash_opaque_token
from medical_ai.services import ServiceResult


class MinimalAnalyticsService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def liveness(self) -> ServiceResult:
        return ServiceResult(data={"status": "alive"})


class FakeAuthenticationService:
    def __init__(self) -> None:
        self.session_token = "server-side-opaque-session"
        self.csrf_token = "csrf-proof"
        self.login_calls = []
        self.resolve_calls = []
        self.logout_calls = []
        self.reject_login = False
        self.active = _active_session()

    def login(self, email, password, *, organization_id=None):
        self.login_calls.append((email, password, organization_id))
        if self.reject_login:
            raise InvalidCredentialsError
        return IssuedSession(
            session_token=self.session_token,
            csrf_token=self.csrf_token,
            active_session=self.active,
        )

    def resolve_session(self, session_token):
        self.resolve_calls.append(session_token)
        return self.active

    def logout(self, session_token, csrf_token):
        self.logout_calls.append((session_token, csrf_token))


class FakeInvitationRegistrationService:
    def __init__(self) -> None:
        self.calls = []
        self.failure: Exception | None = None

    def register_invited_user(self, **kwargs):
        self.calls.append(kwargs)
        if self.failure is not None:
            raise self.failure
        return object()


def _active_session() -> ActiveSession:
    context = AccessContext(
        user_id="user-1",
        organization_id="org-1",
        membership_id="membership-1",
        permissions=frozenset(
            {
                PermissionCode.ANALYTICS_QUERY_EXECUTE,
                PermissionCode.ANALYTICS_SCHEMA_READ,
            }
        ),
        allowed_facility_ids=frozenset({"facility-1"}),
        identity_version=2,
        authorization_version=3,
    )
    now = datetime(2026, 8, 24, 10, 0)
    return ActiveSession(
        session_id="session-1",
        user_id="user-1",
        email="analyst@example.com",
        display_name="Analyst",
        organization_name="Hospital A",
        expires_at=now + timedelta(hours=12),
        idle_expires_at=now + timedelta(minutes=30),
        csrf_token_hash=hash_opaque_token("csrf-proof"),
        access_context=context,
    )


def _client(
    service: FakeAuthenticationService | None = None,
    registration_service: FakeInvitationRegistrationService | None = None,
):
    settings = Settings(
        _env_file=None,
        auth_session_cookie_secure=False,
        auth_session_cookie_name="test_session",
        auth_enforcement_enabled=True,
    )
    auth_service = service or FakeAuthenticationService()
    app = create_app(
        settings=settings,
        analytics_service=MinimalAnalyticsService(settings),
        authentication_service=auth_service,
        invitation_registration_service=registration_service,
    )
    app.config["TESTING"] = True
    return app.test_client(), auth_service


def test_invitation_registration_activates_account_without_returning_secrets() -> None:
    registration_service = FakeInvitationRegistrationService()
    client, _ = _client(registration_service=registration_service)

    response = client.post(
        "/api/v1/auth/registrations",
        json={
            "invitation_token": "single-use-bearer-token",
            "email": "invited@example.com",
            "display_name": "Invited Analyst",
            "password": "correct horse battery staple",
        },
    )

    assert response.status_code == 201
    assert response.get_json()["data"] == {"registered": True}
    serialized = response.get_data(as_text=True)
    assert "single-use-bearer-token" not in serialized
    assert "correct horse battery staple" not in serialized
    call = registration_service.calls[0]
    assert call["email"] == "invited@example.com"
    assert call["display_name"] == "Invited Analyst"
    assert call["request_id"] == response.headers["X-Request-Id"]


def test_registration_rejects_unknown_fields_without_echoing_credentials() -> None:
    registration_service = FakeInvitationRegistrationService()
    client, _ = _client(registration_service=registration_service)

    response = client.post(
        "/api/v1/auth/registrations",
        json={
            "invitation_token": "secret-token",
            "email": "invited@example.com",
            "display_name": "Invited Analyst",
            "password": "correct horse battery staple",
            "role": "administrator",
        },
    )

    assert response.status_code == 400
    assert response.get_json()["error"]["field_errors"] == [
        {"field": "role", "message": "不支持的字段"}
    ]
    assert registration_service.calls == []
    assert "secret-token" not in response.get_data(as_text=True)
    assert "correct horse battery staple" not in response.get_data(as_text=True)


def test_registration_bounds_display_name_before_domain_service() -> None:
    registration_service = FakeInvitationRegistrationService()
    client, _ = _client(registration_service=registration_service)

    response = client.post(
        "/api/v1/auth/registrations",
        json={
            "invitation_token": "single-use-token",
            "email": "invited@example.com",
            "display_name": "x" * 121,
            "password": "correct horse battery staple",
        },
    )

    assert response.status_code == 400
    assert response.get_json()["error"]["field_errors"] == [
        {"field": "display_name", "message": "最多 120 个字符"}
    ]
    assert registration_service.calls == []


def test_invalid_invitation_uses_one_stable_registration_error() -> None:
    registration_service = FakeInvitationRegistrationService()
    registration_service.failure = InvalidInvitationError()
    client, _ = _client(registration_service=registration_service)

    response = client.post(
        "/api/v1/auth/registrations",
        json={
            "invitation_token": "wrong-or-expired-token",
            "email": "invited@example.com",
            "display_name": "Invited Analyst",
            "password": "correct horse battery staple",
        },
    )

    assert response.status_code == 400
    assert response.get_json()["error"] == {
        "code": "INVALID_INVITATION",
        "message": "邀请无效或已过期，请联系管理员重新邀请",
    }


def test_registration_maps_password_policy_to_one_field_error() -> None:
    registration_service = FakeInvitationRegistrationService()
    registration_service.failure = PasswordPolicyError("密码至少需要 12 个字符")
    client, _ = _client(registration_service=registration_service)

    response = client.post(
        "/api/v1/auth/registrations",
        json={
            "invitation_token": "single-use-token",
            "email": "invited@example.com",
            "display_name": "Invited Analyst",
            "password": "short",
        },
    )

    assert response.status_code == 400
    assert response.get_json()["error"] == {
        "code": "PASSWORD_POLICY_VIOLATION",
        "message": "密码不符合安全要求",
        "field_errors": [
            {"field": "password", "message": "密码至少需要 12 个字符"}
        ],
    }


def test_login_sets_securely_scoped_httponly_cookie_without_returning_session_token() -> None:
    client, service = _client()

    response = client.post(
        "/api/v1/auth/sessions",
        json={
            "email": "analyst@example.com",
            "password": "correct horse battery staple",
            "organization_id": "org-1",
        },
    )

    assert response.status_code == 201
    payload = response.get_json()
    assert payload["success"] is True
    assert payload["data"]["csrf_token"] == service.csrf_token
    assert payload["data"]["session"]["user"]["email"] == "analyst@example.com"
    assert service.session_token not in response.get_data(as_text=True)
    cookies = response.headers.getlist("Set-Cookie")
    session_cookie = next(cookie for cookie in cookies if cookie.startswith("test_session="))
    csrf_cookie = next(cookie for cookie in cookies if cookie.startswith("medical_ai_csrf="))
    assert "test_session=server-side-opaque-session" in session_cookie
    assert "HttpOnly" in session_cookie
    assert "SameSite=Lax" in session_cookie
    assert "Path=/api/v1" in session_cookie
    assert "medical_ai_csrf=csrf-proof" in csrf_cookie
    assert "Path=/" in csrf_cookie
    assert "HttpOnly" not in csrf_cookie
    assert response.headers["Cache-Control"] == "no-store"
    assert service.login_calls == [
        ("analyst@example.com", "correct horse battery staple", "org-1")
    ]


def test_current_session_uses_cookie_and_returns_no_secrets() -> None:
    client, service = _client()
    client.set_cookie("test_session", service.session_token, path="/api/v1")

    response = client.get("/api/v1/auth/me")

    assert response.status_code == 200
    assert service.resolve_calls == [service.session_token]
    serialized = response.get_data(as_text=True)
    assert service.session_token not in serialized
    assert "csrf_token_hash" not in serialized
    assert "password" not in serialized


def test_logout_forwards_csrf_header_then_expires_cookie() -> None:
    client, service = _client()
    client.set_cookie("test_session", service.session_token, path="/api/v1")

    response = client.delete(
        "/api/v1/auth/sessions/current",
        headers={"X-CSRF-Token": service.csrf_token},
    )

    assert response.status_code == 200
    assert service.logout_calls == [(service.session_token, service.csrf_token)]
    assert response.get_json()["data"] == {"revoked": True}
    cookies = response.headers.getlist("Set-Cookie")
    assert any("test_session=" in cookie and "Expires=Thu, 01 Jan 1970" in cookie for cookie in cookies)
    assert any("medical_ai_csrf=" in cookie and "Expires=Thu, 01 Jan 1970" in cookie for cookie in cookies)


def test_invalid_credentials_use_one_stable_401_contract() -> None:
    service = FakeAuthenticationService()
    service.reject_login = True
    client, _ = _client(service)

    response = client.post(
        "/api/v1/auth/sessions",
        json={"email": "unknown@example.com", "password": "incorrect password"},
    )

    assert response.status_code == 401
    assert response.get_json()["error"] == {
        "code": "INVALID_CREDENTIALS",
        "message": "邮箱或密码不正确",
    }
    assert service.session_token not in response.get_data(as_text=True)


def test_login_input_validation_does_not_echo_password() -> None:
    client, _ = _client()

    response = client.post("/api/v1/auth/sessions", json={"email": "analyst@example.com"})

    assert response.status_code == 400
    assert response.get_json()["error"]["field_errors"] == [
        {"field": "password", "message": "不能为空"}
    ]
    assert "analyst@example.com" not in response.get_data(as_text=True)


def test_authentication_routes_explicitly_report_disabled_local_mode() -> None:
    settings = Settings(
        _env_file=None,
        auth_session_cookie_secure=False,
        auth_enforcement_enabled=False,
    )
    app = create_app(
        settings=settings,
        analytics_service=MinimalAnalyticsService(settings),
        authentication_service=FakeAuthenticationService(),
    )
    app.config["TESTING"] = True

    response = app.test_client().get("/api/v1/auth/me")

    assert response.status_code == 404
    assert response.get_json()["error"]["code"] == "AUTHENTICATION_DISABLED"
