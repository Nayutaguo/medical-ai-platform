"""Transport-neutral identity and authenticated-session domain models."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from medical_ai.authorization import AccessContext, PermissionCode


USER_INVITATION_PURPOSE = "user_invitation"
INVITATION_ACTIVATE_IDENTITY = "activate_identity"
INVITATION_ACTIVATE_MEMBERSHIP = "activate_membership"
INVITED_MEMBER_DISPLAY_NAME = "Invited user"


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
    actor_identity_version: int | None = None
    actor_authorization_version: int | None = None
    actor_session_id: str | None = None


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
class InvitationRegistrationChallenge:
    """Token-bound credential facts needed before an atomic acceptance.

    The password verifier is an internal security value.  ``repr=False`` keeps
    it out of tracebacks and accidental structured logging.
    """

    activation_mode: str
    password_hash: str | None
    password_algorithm: str | None
    identity_version: int
    user_version: int
    token_version: int
    membership_version: int


@dataclass(frozen=True, slots=True, repr=False)
class InvitationRegistrationPlan:
    """A validated registration completion ready for atomic consumption."""

    token_hash: bytes
    email_normalized: str
    display_name: str
    activation_mode: str
    password_hash: str | None
    password_algorithm: str | None
    expected_password_hash: str | None
    expected_password_algorithm: str | None
    expected_identity_version: int
    expected_user_version: int
    expected_token_version: int
    expected_membership_version: int
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


@dataclass(frozen=True, slots=True)
class AdministrationRole:
    """Non-secret organization role facts exposed to administrators."""

    id: str
    role_key: str
    name: str
    description: str | None
    permissions: tuple[str, ...]
    version: int

    def to_public_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "role_key": self.role_key,
            "name": self.name,
            "description": self.description,
            "permissions": list(self.permissions),
            "version": self.version,
        }


@dataclass(frozen=True, slots=True)
class AdministrationFacility:
    """Organization-owned facility facts safe for administration screens."""

    id: str
    facility_key: str
    display_name: str | None
    status: str
    version: int

    def to_public_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "facility_key": self.facility_key,
            "display_name": self.display_name,
            "status": self.status,
            "version": self.version,
        }


@dataclass(frozen=True, slots=True)
class AdministrationMember:
    """One tenant-bound membership and its explicit governance grants."""

    membership_id: str
    user_id: str
    email: str
    display_name: str
    user_status: str
    membership_status: str
    authorization_version: int
    version: int
    roles: tuple[AdministrationRole, ...] = ()
    facilities: tuple[AdministrationFacility, ...] = ()

    def to_public_dict(self) -> dict[str, object]:
        return {
            "membership_id": self.membership_id,
            "user_id": self.user_id,
            "email": self.email,
            "display_name": self.display_name,
            "user_status": self.user_status,
            "membership_status": self.membership_status,
            "authorization_version": self.authorization_version,
            "version": self.version,
            "roles": [role.to_public_dict() for role in self.roles],
            "facilities": [facility.to_public_dict() for facility in self.facilities],
        }


@dataclass(frozen=True, slots=True)
class AdministrationPage:
    """Bounded keyset page returned by administration repositories."""

    items: tuple[AdministrationMember | AdministrationRole | AdministrationFacility, ...]
    next_cursor: str | None

    def to_public_dict(self) -> dict[str, object]:
        return {
            "items": [item.to_public_dict() for item in self.items],
            "next_cursor": self.next_cursor,
        }


@dataclass(frozen=True, slots=True)
class FacilityCatalogSyncResult:
    """Aggregate-only result of an idempotent organization catalog sync."""

    created_count: int
    existing_count: int

    def to_public_dict(self) -> dict[str, object]:
        return {
            "created_count": self.created_count,
            "existing_count": self.existing_count,
        }
