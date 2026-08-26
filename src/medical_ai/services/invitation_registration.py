"""Invitation-only account activation independent of Flask transport objects."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from medical_ai.audit import AuditActor, AuditActorKind
from medical_ai.identity.models import (
    INVITATION_ACTIVATE_IDENTITY,
    INVITATION_ACTIVATE_MEMBERSHIP,
    INVITED_MEMBER_DISPLAY_NAME,
    InvitationRegistrationPlan,
    IssuedUserInvitation,
    RegistrationResult,
    UserInvitationPlan,
)
from medical_ai.identity.passwords import Argon2idPasswordService
from medical_ai.identity.errors import InvalidInvitationError
from medical_ai.identity.emails import normalize_ascii_email
from medical_ai.identity.ports import InvitationRegistrationRepositoryPort
from medical_ai.identity.tokens import generate_opaque_token, hash_opaque_token


DEFAULT_INVITATION_LIFETIME = timedelta(hours=24)
MINIMUM_INVITATION_LIFETIME = timedelta(minutes=5)
MAXIMUM_INVITATION_LIFETIME = timedelta(days=7)
_INVALID_TOKEN_SENTINEL = "invalid-invitation-token"
_INVALID_EMAIL_SENTINEL = "invalid-invitation-identifier"


class InvitationRegistrationService:
    """Issue administrator-controlled invitations and activate them once.

    Invitation issuance is deliberately not an anonymous self-registration
    operation: every caller must supply a validated audit actor. Authorization
    (normally ``users.manage``) remains the responsibility of the authenticated
    API boundary that calls this transport-neutral service.
    """

    def __init__(
        self,
        repository: InvitationRegistrationRepositoryPort,
        *,
        password_service: Argon2idPasswordService | None = None,
        dummy_password_hash: str | None = None,
    ) -> None:
        self.repository = repository
        self.password_service = password_service or Argon2idPasswordService()
        self._dummy_password_hash = (
            dummy_password_hash
            or self.password_service.hash_password(
                "unusable invitation acceptance password"
            )
        )

    def issue_invitation(
        self,
        *,
        organization_id: str,
        email: str,
        actor: AuditActor,
        lifetime: timedelta = DEFAULT_INVITATION_LIFETIME,
        request_id: str | None = None,
        now: datetime | None = None,
        actor_identity_version: int | None = None,
        actor_authorization_version: int | None = None,
        actor_session_id: str | None = None,
    ) -> IssuedUserInvitation:
        """Create an invited identity and return its bearer token once."""

        normalized_organization_id = _validated_uuid(
            organization_id,
            field_name="organization_id",
        )
        normalized_email = _validated_email(email)
        _validate_invitation_lifetime(lifetime)
        _validate_invitation_actor(actor, normalized_organization_id)
        _validate_actor_version_snapshot(
            actor,
            identity_version=actor_identity_version,
            authorization_version=actor_authorization_version,
            session_id=actor_session_id,
        )
        normalized_request_id = _optional_bounded_text(request_id, "request_id", 128)
        current_time = _utc_now(now)
        expires_at = current_time + lifetime
        opaque_token = generate_opaque_token()
        user_id = str(uuid4())
        membership_id = str(uuid4())
        plan = UserInvitationPlan(
            token_id=str(uuid4()),
            user_id=user_id,
            email=email.strip(),
            email_normalized=normalized_email,
            placeholder_display_name=INVITED_MEMBER_DISPLAY_NAME,
            organization_id=normalized_organization_id,
            membership_id=membership_id,
            identity_version=1,
            token_hash=opaque_token.digest,
            expires_at=expires_at,
            created_at=current_time,
            request_id=normalized_request_id,
            actor_kind=actor.kind.value,
            actor_membership_id=actor.membership_id,
            actor_user_id=actor.user_id,
            actor_identity_version=actor_identity_version,
            actor_authorization_version=actor_authorization_version,
            actor_session_id=actor_session_id,
        )
        persisted = self.repository.create_user_invitation(plan)
        return IssuedUserInvitation(
            token=opaque_token.value,
            user_id=persisted.user_id,
            organization_id=persisted.organization_id,
            membership_id=persisted.membership_id,
            expires_at=expires_at,
        )

    def register_invited_user(
        self,
        *,
        token: str,
        email: str,
        display_name: str,
        password: str,
        request_id: str | None = None,
        now: datetime | None = None,
    ) -> RegistrationResult:
        """Atomically activate the exact identity bound to an invitation.

        Wrong, expired, replayed, cross-email, and stale-identity tokens all
        become the same stable domain error in the repository. Password and
        display-name policy failures remain ordinary field validation errors.
        """

        normalized_display_name = _bounded_text(display_name, "display_name", 120)
        # Apply public password-shape policy before token lookup so a caller
        # cannot distinguish valid and invalid invitation digests with a short
        # password. Existing identities still use the value only as proof.
        self.password_service.policy.validate(password)
        normalized_request_id = _optional_bounded_text(request_id, "request_id", 128)
        current_time = _utc_now(now)

        presented_token = (
            token
            if isinstance(token, str) and 1 <= len(token) <= 512
            else _INVALID_TOKEN_SENTINEL
        )
        normalized_email = _normalized_email_or_sentinel(email)
        token_hash = hash_opaque_token(presented_token)
        challenge = self.repository.find_invitation_registration_challenge(
            token_hash=token_hash,
            email_normalized=normalized_email,
            now=current_time,
        )
        if challenge is None:
            # Keep the invalid-token path on the same Argon2 verification
            # primitive without revealing whether a token or email matched.
            self.password_service.verify_password(
                self._dummy_password_hash,
                password,
            )
            raise InvalidInvitationError

        password_hash: str | None
        password_algorithm: str | None
        if challenge.activation_mode == INVITATION_ACTIVATE_IDENTITY:
            password_hash = self.password_service.hash_password(password)
            password_algorithm = self.password_service.algorithm
        elif challenge.activation_mode == INVITATION_ACTIVATE_MEMBERSHIP:
            if (
                challenge.password_algorithm != self.password_service.algorithm
                or not isinstance(challenge.password_hash, str)
                or not self.password_service.verify_password(
                    challenge.password_hash,
                    password,
                )
            ):
                raise InvalidInvitationError
            # Existing global identity facts are immutable in this flow.  The
            # password is proof of account control, not a replacement value.
            password_hash = None
            password_algorithm = None
        else:
            raise InvalidInvitationError

        plan = InvitationRegistrationPlan(
            token_hash=token_hash,
            email_normalized=normalized_email,
            display_name=normalized_display_name,
            activation_mode=challenge.activation_mode,
            password_hash=password_hash,
            password_algorithm=password_algorithm,
            expected_password_hash=challenge.password_hash,
            expected_password_algorithm=challenge.password_algorithm,
            expected_identity_version=challenge.identity_version,
            expected_user_version=challenge.user_version,
            expected_token_version=challenge.token_version,
            expected_membership_version=challenge.membership_version,
            completed_at=current_time,
            request_id=normalized_request_id,
        )
        return self.repository.consume_user_invitation(plan)


def _validate_invitation_actor(actor: AuditActor, organization_id: str) -> None:
    if not isinstance(actor, AuditActor):
        raise ValueError("actor 必须是受信任的审计主体")
    if actor.organization_id != organization_id:
        raise ValueError("actor 与邀请组织不匹配")


def _validate_actor_version_snapshot(
    actor: AuditActor,
    *,
    identity_version: int | None,
    authorization_version: int | None,
    session_id: str | None,
) -> None:
    """Require user actors to carry the session versions rechecked at commit."""

    if actor.kind is AuditActorKind.SYSTEM:
        if (
            identity_version is not None
            or authorization_version is not None
            or session_id is not None
        ):
            raise ValueError("system actor 不能携带用户会话版本")
        return
    if (
        isinstance(identity_version, bool)
        or not isinstance(identity_version, int)
        or identity_version < 1
        or isinstance(authorization_version, bool)
        or not isinstance(authorization_version, int)
        or authorization_version < 1
        or not isinstance(session_id, str)
        or not session_id.strip()
    ):
        raise ValueError("user actor 必须携带有效的会话版本")


def _validate_invitation_lifetime(lifetime: timedelta) -> None:
    if not isinstance(lifetime, timedelta):
        raise ValueError("lifetime 必须是 timedelta")
    if not MINIMUM_INVITATION_LIFETIME <= lifetime <= MAXIMUM_INVITATION_LIFETIME:
        raise ValueError("邀请有效期必须介于 5 分钟和 7 天之间")


def _validated_uuid(value: str, *, field_name: str) -> str:
    try:
        parsed = UUID(value.strip())
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} 格式不正确") from exc
    return str(parsed)


def _validated_email(value: str) -> str:
    normalized = _normalized_email_or_sentinel(value)
    if normalized == _INVALID_EMAIL_SENTINEL:
        raise ValueError("email 格式不正确")
    return normalized


def _normalized_email_or_sentinel(value: str) -> str:
    return normalize_ascii_email(value) or _INVALID_EMAIL_SENTINEL


def _bounded_text(value: str, field: str, maximum: int) -> str:
    normalized = value.strip() if isinstance(value, str) else ""
    if not normalized:
        raise ValueError(f"{field} 不能为空")
    if len(normalized) > maximum:
        raise ValueError(f"{field} 超过 {maximum} 个字符")
    return normalized


def _optional_bounded_text(value: str | None, field: str, maximum: int) -> str | None:
    if value is None:
        return None
    return _bounded_text(value, field, maximum)


def _utc_now(value: datetime | None) -> datetime:
    current = value or datetime.now(UTC)
    if current.tzinfo is None:
        raise ValueError("邀请时间必须包含时区")
    return current.astimezone(UTC).replace(tzinfo=None)
