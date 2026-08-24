"""Interactive login and opaque server-side session orchestration."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from medical_ai.authorization import AccessContext
from medical_ai.identity.errors import (
    AuthenticationRequiredError,
    CsrfValidationError,
    InvalidCredentialsError,
    MembershipUnavailableError,
    OrganizationSelectionRequiredError,
)
from medical_ai.identity.models import ActiveSession, IssuedSession, MembershipGrant, NewSession
from medical_ai.identity.passwords import Argon2idPasswordService
from medical_ai.identity.ports import IdentityRepositoryPort
from medical_ai.identity.tokens import generate_opaque_token, hash_opaque_token, verify_opaque_token


class AuthenticationService:
    """Authenticate users and issue, resolve, rotate, or revoke sessions."""

    def __init__(
        self,
        repository: IdentityRepositoryPort,
        *,
        password_service: Argon2idPasswordService | None = None,
        idle_timeout: timedelta = timedelta(minutes=30),
        absolute_timeout: timedelta = timedelta(hours=12),
        touch_interval: timedelta = timedelta(minutes=5),
        dummy_password_hash: str | None = None,
    ) -> None:
        if idle_timeout <= timedelta(0) or absolute_timeout <= timedelta(0):
            raise ValueError("Session timeouts must be positive")
        if idle_timeout > absolute_timeout:
            raise ValueError("Idle timeout cannot exceed absolute timeout")
        self.repository = repository
        self.password_service = password_service or Argon2idPasswordService()
        self.idle_timeout = idle_timeout
        self.absolute_timeout = absolute_timeout
        self.touch_interval = touch_interval
        self._dummy_password_hash = dummy_password_hash or self.password_service.hash_password(
            "unusable dummy authentication password"
        )

    def login(
        self,
        email: str,
        password: str,
        *,
        organization_id: str | None = None,
        now: datetime | None = None,
    ) -> IssuedSession:
        """Verify credentials and persist only digests for a new session."""

        current_time = _utc_now(now)
        normalized_email = normalize_email(email)
        user = self.repository.find_user_by_normalized_email(normalized_email)
        eligible_user = bool(
            user
            and user.status == "active"
            and user.password_hash
            and user.password_algorithm == self.password_service.algorithm
        )
        verifier = user.password_hash if eligible_user and user else self._dummy_password_hash
        password_valid = self.password_service.verify_password(verifier, password)

        if user is None or not eligible_user or not password_valid:
            raise InvalidCredentialsError

        membership = _select_membership(
            self.repository.list_active_memberships(user.user_id),
            organization_id,
        )
        session_token = generate_opaque_token()
        csrf_token = generate_opaque_token()
        absolute_expiry = current_time + self.absolute_timeout
        idle_expiry = min(current_time + self.idle_timeout, absolute_expiry)
        session_id = str(uuid4())
        new_session = NewSession(
            session_id=session_id,
            user_id=user.user_id,
            membership_id=membership.membership_id,
            organization_id=membership.organization_id,
            session_token_hash=session_token.digest,
            csrf_token_hash=csrf_token.digest,
            expires_at=absolute_expiry,
            idle_expires_at=idle_expiry,
            identity_version=user.auth_version,
            authorization_version=membership.authorization_version,
        )
        self.repository.create_session(new_session, logged_in_at=current_time)

        context = AccessContext(
            user_id=user.user_id,
            organization_id=membership.organization_id,
            membership_id=membership.membership_id,
            permissions=membership.permissions,
            allowed_facility_ids=membership.allowed_facility_ids,
            identity_version=user.auth_version,
            authorization_version=membership.authorization_version,
        )
        active = ActiveSession(
            session_id=session_id,
            user_id=user.user_id,
            email=user.email,
            display_name=user.display_name,
            organization_name=membership.organization_name,
            expires_at=absolute_expiry,
            idle_expires_at=idle_expiry,
            csrf_token_hash=csrf_token.digest,
            access_context=context,
        )
        return IssuedSession(
            session_token=session_token.value,
            csrf_token=csrf_token.value,
            active_session=active,
        )

    def resolve_session(self, session_token: str, *, now: datetime | None = None) -> ActiveSession:
        """Resolve and lazily extend a valid session's idle expiry."""

        if not session_token:
            raise AuthenticationRequiredError
        current_time = _utc_now(now)
        session = self.repository.find_active_session(
            hash_opaque_token(session_token),
            now=current_time,
        )
        if session is None:
            raise AuthenticationRequiredError

        new_idle_expiry = min(current_time + self.idle_timeout, session.expires_at)
        last_touch_boundary = session.idle_expires_at - self.idle_timeout + self.touch_interval
        if current_time >= last_touch_boundary and new_idle_expiry > session.idle_expires_at:
            self.repository.touch_session(
                session.session_id,
                seen_at=current_time,
                idle_expires_at=new_idle_expiry,
            )
            return replace(session, idle_expires_at=new_idle_expiry)
        return session

    def require_csrf(self, session: ActiveSession, csrf_token: str) -> None:
        """Validate the request CSRF token against its server-side digest."""

        if not csrf_token or not verify_opaque_token(csrf_token, session.csrf_token_hash):
            raise CsrfValidationError

    def logout(
        self,
        session_token: str,
        csrf_token: str,
        *,
        now: datetime | None = None,
    ) -> None:
        """Validate CSRF proof then revoke the server-side session."""

        current_time = _utc_now(now)
        session = self.resolve_session(session_token, now=now)
        self.require_csrf(session, csrf_token)
        self.repository.revoke_session(session.session_id, revoked_at=current_time)


def normalize_email(email: str) -> str:
    """Normalize an email identifier for unique lookup without logging it."""

    normalized = email.strip().casefold() if isinstance(email, str) else ""
    if not normalized or len(normalized) > 320 or "@" not in normalized:
        # Keep login responses indistinguishable from unknown identifiers.
        return "invalid-login-identifier"
    return normalized


def _select_membership(
    memberships: list[MembershipGrant],
    organization_id: str | None,
) -> MembershipGrant:
    active = [membership for membership in memberships if membership.status == "active"]
    if organization_id:
        for membership in active:
            if membership.organization_id == organization_id:
                return membership
        raise MembershipUnavailableError
    if not active:
        raise MembershipUnavailableError
    if len(active) > 1:
        raise OrganizationSelectionRequiredError
    return active[0]


def _utc_now(value: datetime | None) -> datetime:
    current = value or datetime.now(UTC)
    if current.tzinfo is None:
        raise ValueError("Authentication timestamps must be timezone-aware")
    return current.astimezone(UTC).replace(tzinfo=None)
