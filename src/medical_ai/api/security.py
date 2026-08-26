"""Shared request authentication helpers for versioned HTTP adapters."""

from __future__ import annotations

from datetime import timedelta
from typing import cast

from flask import current_app, g, request

from medical_ai.authorization import AccessContext
from medical_ai.config import Settings
from medical_ai.identity.models import ActiveSession
from medical_ai.rate_limit import LoginRateLimiter, RedisLoginRateLimiter
from medical_ai.repositories import IdentityRepository
from medical_ai.services import AuthenticationService
from medical_ai.services.governance_administration import GovernanceAdministrationService
from medical_ai.services.invitation_registration import InvitationRegistrationService


def authentication_service() -> AuthenticationService:
    """Return the app-scoped authentication service, creating it lazily."""

    service = current_app.extensions.get("authentication_service")
    if service is None:
        settings = _settings()
        service = AuthenticationService(
            IdentityRepository(settings=settings),
            idle_timeout=timedelta(minutes=settings.auth_session_idle_minutes),
            absolute_timeout=timedelta(hours=settings.auth_session_absolute_hours),
            touch_interval=timedelta(minutes=settings.auth_session_touch_minutes),
            password_reset_lifetime=timedelta(
                minutes=settings.auth_password_reset_minutes
            ),
        )
        current_app.extensions["authentication_service"] = service
    return cast(AuthenticationService, service)


def invitation_registration_service() -> InvitationRegistrationService:
    """Return the app-scoped invitation registration service lazily."""

    service = current_app.extensions.get("invitation_registration_service")
    if service is None:
        service = InvitationRegistrationService(IdentityRepository(settings=_settings()))
        current_app.extensions["invitation_registration_service"] = service
    return cast(InvitationRegistrationService, service)


def governance_administration_service() -> GovernanceAdministrationService:
    """Return the app-scoped tenant administration service lazily."""

    service = current_app.extensions.get("governance_administration_service")
    if service is None:
        repository = IdentityRepository(settings=_settings())
        service = GovernanceAdministrationService(
            repository,
            invitation_service=InvitationRegistrationService(repository),
        )
        current_app.extensions["governance_administration_service"] = service
    return cast(GovernanceAdministrationService, service)


def login_rate_limiter() -> LoginRateLimiter | None:
    """Return the injected or lazily constructed shared login limiter.

    Building the Flask app never imports redis-py or opens a Redis connection.
    The first enabled login request creates the client; its Lua call performs
    the first network operation and fails closed when Redis is unavailable.
    """

    limiter = current_app.extensions.get("login_rate_limiter")
    if limiter is not None:
        return cast(LoginRateLimiter, limiter)

    settings = _settings()
    if not settings.rate_limit_enabled:
        return None

    limiter = RedisLoginRateLimiter.from_url(
        settings.redis_url,
        key_secret=settings.rate_limit_key_secret,
        ip_attempt_limit=settings.login_rate_limit_ip_attempts,
        account_attempt_limit=settings.login_rate_limit_account_attempts,
        window_seconds=settings.login_rate_limit_window_seconds,
    )
    current_app.extensions["login_rate_limiter"] = limiter
    return limiter


def resolve_request_session(*, require_csrf: bool = False) -> ActiveSession:
    """Resolve the opaque cookie once and optionally require its CSRF proof."""

    active = getattr(g, "active_session", None)
    if active is None:
        settings = _settings()
        token = request.cookies.get(settings.auth_session_cookie_name, "")
        active = authentication_service().resolve_session(
            token if isinstance(token, str) else ""
        )
        g.active_session = active

    active = cast(ActiveSession, active)
    if require_csrf:
        settings = _settings()
        authentication_service().require_csrf(
            active,
            request.headers.get(settings.auth_csrf_header_name, ""),
        )
    return active


def access_context(*, require_csrf: bool = False) -> AccessContext:
    """Return the immutable access context for an authenticated request."""

    return resolve_request_session(require_csrf=require_csrf).access_context


def _settings() -> Settings:
    return cast(Settings, current_app.extensions["settings"])
