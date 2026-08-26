"""Persistence port consumed by the authentication application service."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from medical_ai.authorization import AccessContext
from medical_ai.identity.models import (
    ActiveSession,
    AdministrationAuditPage,
    AdministrationMember,
    AdministrationPage,
    AdministrationRole,
    BootstrapPlan,
    BootstrapResult,
    FacilityCatalogSyncResult,
    InvitationRegistrationChallenge,
    InvitationRegistrationPlan,
    MembershipGrant,
    NewSession,
    PasswordChangePlan,
    PasswordResetChallenge,
    PasswordResetPlan,
    PasswordResetTokenPlan,
    PersistedUserInvitation,
    RegistrationResult,
    UserInvitationPlan,
    UserCredential,
)


class IdentityRepositoryPort(Protocol):
    """Minimal durable operations needed by interactive authentication."""

    def find_user_by_normalized_email(self, normalized_email: str) -> UserCredential | None:
        ...

    def find_user_by_id(self, user_id: str) -> UserCredential | None:
        ...

    def list_active_memberships(self, user_id: str) -> list[MembershipGrant]:
        ...

    def create_session(self, session: NewSession, *, logged_in_at: datetime) -> None:
        ...

    def find_active_session(self, token_hash: bytes, *, now: datetime) -> ActiveSession | None:
        ...

    def touch_session(
        self,
        session_id: str,
        *,
        seen_at: datetime,
        idle_expires_at: datetime,
    ) -> None:
        ...

    def revoke_session(self, session_id: str, *, revoked_at: datetime) -> None:
        ...

    def change_password(self, plan: PasswordChangePlan) -> None:
        ...

    def create_password_reset_token(self, plan: PasswordResetTokenPlan) -> bool:
        ...

    def find_password_reset_challenge(
        self,
        *,
        token_hash: bytes,
        now: datetime,
    ) -> PasswordResetChallenge | None:
        ...

    def consume_password_reset(self, plan: PasswordResetPlan) -> None:
        ...


class IdentityBootstrapRepositoryPort(Protocol):
    """Atomic persistence boundary for the one-time first administrator."""

    def bootstrap_first_administrator(self, plan: BootstrapPlan) -> BootstrapResult:
        ...


class InvitationRegistrationRepositoryPort(Protocol):
    """Atomic persistence operations for invitation-only registration."""

    def create_user_invitation(
        self,
        plan: UserInvitationPlan,
    ) -> PersistedUserInvitation:
        ...

    def find_invitation_registration_challenge(
        self,
        *,
        token_hash: bytes,
        email_normalized: str,
        now: datetime,
    ) -> InvitationRegistrationChallenge | None:
        ...

    def consume_user_invitation(
        self,
        plan: InvitationRegistrationPlan,
    ) -> RegistrationResult:
        ...


class GovernanceAdministrationRepositoryPort(Protocol):
    """Tenant-bound administration queries and atomic mutation operations."""

    def list_administration_members(
        self,
        organization_id: str,
        *,
        cursor: str | None,
        limit: int,
    ) -> AdministrationPage:
        ...

    def list_administration_roles(
        self,
        organization_id: str,
        *,
        cursor: str | None,
        limit: int,
    ) -> AdministrationPage:
        ...

    def list_administration_facilities(
        self,
        organization_id: str,
        *,
        cursor: str | None,
        limit: int,
    ) -> AdministrationPage:
        ...

    def list_administration_permissions(
        self,
        *,
        cursor: str | None,
        limit: int,
    ) -> AdministrationPage:
        ...

    def create_administration_role(
        self,
        *,
        context: AccessContext,
        role_key: str,
        name: str,
        description: str | None,
        permission_ids: tuple[str, ...],
        request_id: str | None,
        occurred_at: datetime,
    ) -> AdministrationRole:
        ...

    def update_administration_role(
        self,
        *,
        context: AccessContext,
        role_id: str,
        name: str,
        description: str | None,
        permission_ids: tuple[str, ...],
        expected_version: int,
        request_id: str | None,
        occurred_at: datetime,
    ) -> AdministrationRole:
        ...

    def delete_administration_role(
        self,
        *,
        context: AccessContext,
        role_id: str,
        expected_version: int,
        request_id: str | None,
        occurred_at: datetime,
    ) -> None:
        ...

    def list_administration_audit_events(
        self,
        context: AccessContext,
        *,
        cursor: str | None,
        limit: int,
        action: str | None,
        outcome: str | None,
        read_at: datetime,
    ) -> AdministrationAuditPage:
        ...

    def replace_membership_roles(
        self,
        *,
        context: AccessContext,
        membership_id: str,
        role_ids: tuple[str, ...],
        expected_version: int,
        request_id: str | None,
        occurred_at: datetime,
    ) -> AdministrationMember:
        ...

    def replace_membership_facility_scope(
        self,
        *,
        context: AccessContext,
        membership_id: str,
        facility_ids: tuple[str, ...],
        expected_version: int,
        request_id: str | None,
        occurred_at: datetime,
    ) -> AdministrationMember:
        ...

    def update_membership_status(
        self,
        *,
        context: AccessContext,
        membership_id: str,
        status: str,
        expected_version: int,
        request_id: str | None,
        occurred_at: datetime,
    ) -> AdministrationMember:
        ...

    def sync_organization_facilities(
        self,
        *,
        context: AccessContext,
        request_id: str | None,
        occurred_at: datetime,
    ) -> FacilityCatalogSyncResult:
        ...
