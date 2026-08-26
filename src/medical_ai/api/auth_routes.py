"""Versioned browser-authentication routes using opaque server-side sessions."""

from __future__ import annotations

from typing import Any, cast

from flask import Blueprint, current_app, request

from medical_ai.api.errors import APIError
from medical_ai.api.responses import current_request_id, success_response
from medical_ai.api.security import (
    authentication_service,
    invitation_registration_service,
    login_rate_limiter,
    resolve_request_session,
)
from medical_ai.config import Settings
from medical_ai.services import AuthenticationService
from medical_ai.services.authentication import normalize_email

auth_api = Blueprint("auth_api", __name__, url_prefix="/api/v1/auth")


@auth_api.post("/registrations")
def create_registration():
    """Activate an invited organization member without granting data access."""

    _require_authentication_enabled()
    payload = _json_object()
    allowed_fields = {"invitation_token", "email", "display_name", "password"}
    unknown_fields = sorted(set(payload) - allowed_fields)
    if unknown_fields:
        raise APIError(
            "INVALID_REQUEST",
            "请求包含不支持的字段",
            field_errors=[
                {"field": field, "message": "不支持的字段"}
                for field in unknown_fields
            ],
        )

    invitation_token = _required_string(payload, "invitation_token", maximum=512)
    email = _required_string(payload, "email", maximum=320)
    display_name = _required_string(payload, "display_name", maximum=480)
    if len(display_name) > 120:
        raise APIError(
            "INVALID_REQUEST",
            "display_name 超过长度限制",
            field_errors=[{"field": "display_name", "message": "最多 120 个字符"}],
        )
    password = _required_string(payload, "password", maximum=1024)

    limiter = login_rate_limiter()
    if limiter is not None:
        limiter.check(
            source_ip=request.remote_addr or "",
            normalized_account=normalize_email(email),
        )

    registration = invitation_registration_service().register_invited_user(
        token=invitation_token,
        email=email,
        display_name=display_name,
        password=password,
        request_id=current_request_id(),
    )
    return success_response(
        {
            "registered": True,
            "organization_id": registration.organization_id,
        },
        status=201,
    )


@auth_api.post("/sessions")
def create_session():
    """Authenticate credentials and set an HttpOnly opaque-session cookie."""

    _require_authentication_enabled()
    payload = _json_object()
    email = _required_string(payload, "email", maximum=320)
    password = _required_string(payload, "password", maximum=1024)
    organization_id = payload.get("organization_id")
    if organization_id is not None and (not isinstance(organization_id, str) or not organization_id.strip()):
        raise APIError(
            "INVALID_REQUEST",
            "organization_id 必须是非空字符串",
            field_errors=[{"field": "organization_id", "message": "必须是非空字符串"}],
        )

    limiter = login_rate_limiter()
    if limiter is not None:
        # Deliberately use Flask's direct peer address. Forwarded headers are
        # not trusted unless a separately reviewed proxy boundary is added.
        limiter.check(
            source_ip=request.remote_addr or "",
            normalized_account=normalize_email(email),
        )

    issued = _service().login(
        email,
        password,
        organization_id=organization_id.strip() if isinstance(organization_id, str) else None,
    )
    settings = _settings()
    response, status = success_response(
        {
            "session": issued.active_session.to_public_dict(),
            # CSRF proof is not an authentication credential. The raw session
            # token remains exclusively in the HttpOnly cookie.
            "csrf_token": issued.csrf_token,
            "csrf_cookie_name": settings.auth_csrf_cookie_name,
            "csrf_header_name": settings.auth_csrf_header_name,
        },
        status=201,
    )
    response.set_cookie(
        settings.auth_session_cookie_name,
        issued.session_token,
        max_age=settings.auth_session_absolute_hours * 3600,
        secure=settings.auth_session_cookie_secure,
        httponly=True,
        samesite=settings.auth_session_cookie_samesite,
        path="/api/v1",
    )
    response.set_cookie(
        settings.auth_csrf_cookie_name,
        issued.csrf_token,
        max_age=settings.auth_session_absolute_hours * 3600,
        secure=settings.auth_session_cookie_secure,
        httponly=False,
        samesite=settings.auth_session_cookie_samesite,
        # The SPA is served from ``/`` and must be able to restore the CSRF
        # proof into memory after a page refresh.  This cookie is intentionally
        # readable by JavaScript; the authentication credential remains the
        # separate HttpOnly session cookie scoped to ``/api/v1``.
        path="/",
    )
    response.headers["Cache-Control"] = "no-store"
    return response, status


@auth_api.get("/me")
def current_session():
    """Return the current non-secret user, organization, and permission view."""

    _require_authentication_enabled()
    active = _service().resolve_session(_session_cookie())
    settings = _settings()
    response, status = success_response(
        {
            "session": active.to_public_dict(),
            "csrf_cookie_name": settings.auth_csrf_cookie_name,
            "csrf_header_name": settings.auth_csrf_header_name,
        }
    )
    response.headers["Cache-Control"] = "no-store"
    return response, status


@auth_api.delete("/sessions/current")
def delete_session():
    """Revoke the current session after validating CSRF proof."""

    _require_authentication_enabled()
    settings = _settings()
    _service().logout(
        _session_cookie(),
        request.headers.get(settings.auth_csrf_header_name, ""),
    )
    response, status = success_response({"revoked": True})
    response.delete_cookie(
        settings.auth_session_cookie_name,
        secure=settings.auth_session_cookie_secure,
        httponly=True,
        samesite=settings.auth_session_cookie_samesite,
        path="/api/v1",
    )
    response.delete_cookie(
        settings.auth_csrf_cookie_name,
        secure=settings.auth_session_cookie_secure,
        httponly=False,
        samesite=settings.auth_session_cookie_samesite,
        path="/",
    )
    response.headers["Cache-Control"] = "no-store"
    return response, status


@auth_api.post("/password/change")
def change_password():
    """Change the authenticated identity password and revoke all sessions."""

    _require_authentication_enabled()
    payload = _json_object()
    _reject_unknown_fields(payload, {"current_password", "new_password"})
    session = resolve_request_session(require_csrf=True)
    _service().change_password(
        session,
        _required_string(payload, "current_password", maximum=1024),
        _required_string(payload, "new_password", maximum=1024),
        request_id=current_request_id(),
    )
    response, status = success_response({"changed": True})
    _expire_authentication_cookies(response)
    response.headers["Cache-Control"] = "no-store"
    return response, status


@auth_api.post("/password-reset-requests")
def create_password_reset_request():
    """Return one enumeration-safe acceptance envelope for reset requests."""

    _require_authentication_enabled()
    payload = _json_object()
    _reject_unknown_fields(payload, {"email", "organization_id"})
    email = _required_string(payload, "email", maximum=320)
    organization_id = payload.get("organization_id")
    if organization_id is not None:
        if not isinstance(organization_id, str) or not organization_id.strip():
            raise APIError(
                "INVALID_REQUEST",
                "organization_id 必须是非空字符串",
                field_errors=[{"field": "organization_id", "message": "必须是非空字符串"}],
            )
        organization_id = organization_id.strip()
        if len(organization_id) > 36:
            raise APIError(
                "INVALID_REQUEST",
                "organization_id 超过长度限制",
                field_errors=[{"field": "organization_id", "message": "最多 36 个字符"}],
            )

    limiter = login_rate_limiter()
    if limiter is not None:
        limiter.check(
            source_ip=request.remote_addr or "",
            normalized_account=normalize_email(email),
        )
    issued = _service().request_password_reset(
        email,
        organization_id=organization_id,
        request_id=current_request_id(),
    )
    data: dict[str, object] = {"accepted": True}
    if _settings().auth_dev_expose_password_reset_token and issued is not None:
        data.update(
            {
                "reset_token": issued.token,
                "expires_at": issued.expires_at.isoformat(),
                "organization_id": issued.organization_id,
            }
        )
    response, status = success_response(data, status=202)
    response.headers["Cache-Control"] = "no-store"
    return response, status


@auth_api.post("/password-resets")
def reset_password():
    """Consume a one-time reset token bound to email, organization, and version."""

    _require_authentication_enabled()
    payload = _json_object()
    _reject_unknown_fields(
        payload,
        {"token", "email", "organization_id", "new_password"},
    )
    email = _required_string(payload, "email", maximum=320)
    limiter = login_rate_limiter()
    if limiter is not None:
        limiter.check(
            source_ip=request.remote_addr or "",
            normalized_account=normalize_email(email),
        )
    _service().reset_password(
        _required_string(payload, "token", maximum=512),
        email,
        _required_string(payload, "organization_id", maximum=36),
        _required_string(payload, "new_password", maximum=1024),
        request_id=current_request_id(),
    )
    response, status = success_response({"reset": True})
    response.headers["Cache-Control"] = "no-store"
    return response, status


def _service() -> AuthenticationService:
    return authentication_service()


def _settings() -> Settings:
    return cast(Settings, current_app.extensions["settings"])


def _session_cookie() -> str:
    value = request.cookies.get(_settings().auth_session_cookie_name, "")
    return value if isinstance(value, str) else ""


def _expire_authentication_cookies(response) -> None:
    settings = _settings()
    response.delete_cookie(
        settings.auth_session_cookie_name,
        secure=settings.auth_session_cookie_secure,
        httponly=True,
        samesite=settings.auth_session_cookie_samesite,
        path="/api/v1",
    )
    response.delete_cookie(
        settings.auth_csrf_cookie_name,
        secure=settings.auth_session_cookie_secure,
        httponly=False,
        samesite=settings.auth_session_cookie_samesite,
        path="/",
    )


def _require_authentication_enabled() -> None:
    """Keep the optional local-demo posture explicit and machine-readable."""

    if not _settings().auth_enforcement_enabled:
        raise APIError(
            "AUTHENTICATION_DISABLED",
            "当前本地开发环境未启用身份认证",
            status=404,
        )


def _json_object() -> dict[str, Any]:
    if not request.is_json:
        raise APIError("UNSUPPORTED_MEDIA_TYPE", "请求必须使用 application/json", status=415)
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise APIError("INVALID_REQUEST", "请求体必须是 JSON 对象")
    return payload


def _required_string(payload: dict[str, Any], field: str, *, maximum: int) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise APIError(
            "INVALID_REQUEST",
            f"{field} 不能为空",
            field_errors=[{"field": field, "message": "不能为空"}],
        )
    if len(value.encode("utf-8")) > maximum:
        raise APIError(
            "INVALID_REQUEST",
            f"{field} 超过长度限制",
            field_errors=[{"field": field, "message": f"最多 {maximum} 个 UTF-8 字节"}],
        )
    return value if field in {"password", "current_password", "new_password"} else value.strip()


def _reject_unknown_fields(payload: dict[str, Any], allowed_fields: set[str]) -> None:
    unknown_fields = sorted(set(payload) - allowed_fields)
    if unknown_fields:
        raise APIError(
            "INVALID_REQUEST",
            "请求包含不支持的字段",
            field_errors=[
                {"field": field, "message": "不支持的字段"}
                for field in unknown_fields
            ],
        )
