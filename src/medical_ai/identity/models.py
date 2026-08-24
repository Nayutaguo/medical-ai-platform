"""Transport-neutral identity and authenticated-session domain models."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from medical_ai.authorization import AccessContext, PermissionCode


USER_INVITATION_PURPOSE = "user_invitation"


@dataclass(frozen=True, slots=True, repr=False)
class UserCredential:
    """Credential facts loaded for login without exposing them to the API."""

    user_id: str
    email: str
    display_name: str
    status: str
    password_hash: str | None
    password_algorithm: str | None
    auth_version: int


@dataclass(frozen=True, slots=True)
class MembershipGrant:
    """One active organization membership and its effective grants."""

    membership_id: str
    organization_id: str
    organization_name: str
    status: str
    authorization_version: int
    permissions: frozenset[PermissionCode]
    allowed_facility_ids: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True, repr=False)
class NewSession:
    """Hash-only session values ready for durable persistence."""

    session_id: str
    user_id: str
    membership_id: str
    organization_id: str
    session_token_hash: bytes
    csrf_token_hash: bytes
    expires_at: datetime
    idle_expires_at: datetime
    identity_version: int
    authorization_version: int


@dataclass(frozen=True, slots=True, repr=False)
class ActiveSession:
    """A validated server-side session reconstructed from durable state."""

    session_id: str
    user_id: str
    email: str
    display_name: str
    organization_name: str
    expires_at: datetime
    idle_expires_at: datetime
    csrf_token_hash: bytes
    access_context: AccessContext

    def to_public_dict(self) -> dict[str, object]:
        """Return the non-secret shape exposed by current-session APIs."""

        return {
            "user": {
                "id": self.user_id,
                "email": self.email,
                "display_name": self.display_name,
            },
            "organization": {
                "id": self.access_context.organization_id,
                "name": self.organization_name,
                "membership_id": self.access_context.membership_id,
            },
            "permissions": sorted(permission.value for permission in self.access_context.permissions),
            "expires_at": self.expires_at.isoformat(),
            "idle_expires_at": self.idle_expires_at.isoformat(),
        }


@dataclass(frozen=True, slots=True, repr=False)
class IssuedSession:
    """A newly issued session; raw tokens exist only in this return value."""

    session_token: str
    csrf_token: str
    active_session: ActiveSession


@dataclass(frozen=True, slots=True)
class BootstrapRole:
    """One built-in role and the explicit permissions assigned to it."""

    role_id: str
    role_key: str
    name: str
    permissions: frozenset[PermissionCode]
    assign_to_bootstrap_membership: bool = False


@dataclass(frozen=True, slots=True, repr=False)
class BootstrapPlan:
    """Complete first-organization state persisted in one transaction."""

    user_id: str
    email: str
    email_normalized: str
    display_name: str
    password_hash: str
    organization_id: str
    organization_name: str
    organization_slug: str
    membership_id: str
    permission_ids: tuple[tuple[PermissionCode, str], ...]
    roles: tuple[BootstrapRole, ...]


@dataclass(frozen=True, slots=True)
class BootstrapResult:
    """Non-secret identifiers returned after successful first bootstrap."""

    user_id: str
    organization_id: str
    membership_id: str


@dataclass(frozen=True, slots=True, repr=False)
class UserInvitationPlan:
    """A tenant-bound invitation persisted without its raw bearer token."""

    token_id: str
    user_id: str
    email: str
    email_normalized: str
    placeholder_display_name: str
    organization_id: str
    membership_id: str
    identity_version: int
    token_hash: bytes
    expires_at: datetime
    created_at: datetime
    request_id: str | None
    actor_kind: str
    actor_membership_id: str | None
    actor_user_id: str | None


@dataclass(frozen=True, slots=True)
class PersistedUserInvitation:
    """Actual identity context used for a new or safely reissued invitation."""

    user_id: str
    organization_id: str
    membership_id: str


@dataclass(frozen=True, slots=True, repr=False)
class IssuedUserInvitation:
    """An issued invitation whose raw token is returned exactly once."""

    token: str
    user_id: str
    organization_id: str
    membership_id: str
    expires_at: datetime


@dataclass(frozen=True, slots=True, repr=False)
class InvitationRegistrationPlan:
    """A validated registration completion ready for atomic consumption."""

    token_hash: bytes
    email_normalized: str
    display_name: str
    password_hash: str
    password_algorithm: str
    completed_at: datetime
    request_id: str | None


@dataclass(frozen=True, slots=True)
class RegistrationResult:
    """Non-secret identity facts returned after invitation activation."""

    user_id: str
    organization_id: str
    membership_id: str
    email: str
    display_name: str
