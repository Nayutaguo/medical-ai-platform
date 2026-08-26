"""Immutable authorization domain models."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class PermissionCode(StrEnum):
    """Stable, additive permission codes used by every application adapter."""

    ANALYTICS_QUERY_EXECUTE = "analytics.query.execute"
    ANALYTICS_AGENT_EXECUTE = "analytics.agent.execute"
    ANALYTICS_SCHEMA_READ = "analytics.schema.read"
    ANALYTICS_DISTINCT_READ = "analytics.distinct.read"
    USERS_MANAGE = "users.manage"
    ROLES_ASSIGN = "roles.assign"
    AUDIT_READ = "audit.read"
    IMPORTS_CREATE = "imports.create"


@dataclass(frozen=True, slots=True)
class AccessContext:
    """Authorization facts for one authenticated membership.

    The context intentionally contains no credential, password, session token, or
    other authentication secret. Collections are normalized to ``frozenset`` so
    callers cannot mutate the effective authorization state after construction.
    """

    user_id: str
    organization_id: str
    membership_id: str
    permissions: frozenset[PermissionCode]
    allowed_facility_ids: frozenset[str]
    identity_version: int
    authorization_version: int
    session_id: str | None = None

    def __post_init__(self) -> None:
        """Normalize immutable collections and reject malformed domain state."""

        object.__setattr__(self, "user_id", _required_identifier(self.user_id, "user_id"))
        object.__setattr__(
            self,
            "organization_id",
            _required_identifier(self.organization_id, "organization_id"),
        )
        object.__setattr__(
            self,
            "membership_id",
            _required_identifier(self.membership_id, "membership_id"),
        )

        normalized_permissions = frozenset(
            PermissionCode(permission) for permission in self.permissions
        )
        normalized_facilities = frozenset(
            _required_identifier(facility_id, "allowed_facility_ids")
            for facility_id in self.allowed_facility_ids
        )
        object.__setattr__(self, "permissions", normalized_permissions)
        object.__setattr__(self, "allowed_facility_ids", normalized_facilities)

        if self.identity_version < 1:
            raise ValueError("identity_version must be greater than or equal to 1")
        if self.authorization_version < 1:
            raise ValueError("authorization_version must be greater than or equal to 1")
        if self.session_id is not None:
            object.__setattr__(
                self,
                "session_id",
                _required_identifier(self.session_id, "session_id"),
            )

    def has_permission(self, permission: PermissionCode) -> bool:
        """Return whether the context contains exactly ``permission``."""

        return PermissionCode(permission) in self.permissions


def _required_identifier(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()
