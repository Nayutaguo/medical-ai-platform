"""Persistence port consumed by the authentication application service."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from medical_ai.identity.models import (
    ActiveSession,
    BootstrapPlan,
    BootstrapResult,
    InvitationRegistrationPlan,
    RegistrationResult,
    MembershipGrant,
    NewSession,
    PersistedUserInvitation,
    UserInvitationPlan,
    UserCredential,
)


class IdentityRepositoryPort(Protocol):
    """Minimal durable operations needed by interactive authentication."""

    def find_user_by_normalized_email(self, normalized_email: str) -> UserCredential | None:
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

    def consume_user_invitation(
        self,
        plan: InvitationRegistrationPlan,
    ) -> RegistrationResult:
        ...
