"""Tenant-bound identity, role, and facility administration orchestration."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from medical_ai.authorization import AccessContext, PermissionCode, require_permission
from medical_ai.authorization.errors import PermissionDeniedError
from medical_ai.identity.errors import (
    AdministrationSelfLockoutError,
    AdministrationValidationError,
)
from medical_ai.identity.emails import normalize_ascii_email
from medical_ai.identity.models import (
    AdministrationMember,
    AdministrationPage,
    FacilityCatalogSyncResult,
    IssuedUserInvitation,
)
from medical_ai.identity.ports import GovernanceAdministrationRepositoryPort
from medical_ai.services.invitation_registration import InvitationRegistrationService


DEFAULT_PAGE_LIMIT = 50
MAX_PAGE_LIMIT = 100
MAX_ROLE_ASSIGNMENTS = 16
MAX_FACILITY_ASSIGNMENTS = 1_000
ALLOWED_MEMBERSHIP_STATUSES = frozenset({"active", "suspended"})


class GovernanceAdministrationService:
    """Apply exact permissions before tenant-scoped administration persistence."""

    def __init__(
        self,
        repository: GovernanceAdministrationRepositoryPort,
        *,
        invitation_service: InvitationRegistrationService,
    ) -> None:
        self.repository = repository
        self.invitation_service = invitation_service

    def list_members(
        self,
        context: AccessContext,
        *,
        cursor: str | None = None,
        limit: int = DEFAULT_PAGE_LIMIT,
    ) -> AdministrationPage:
        _require_any_permission(
            context,
            PermissionCode.USERS_MANAGE,
            PermissionCode.ROLES_ASSIGN,
        )
        return self.repository.list_administration_members(
            context.organization_id,
            cursor=_validated_cursor(cursor),
            limit=_validated_limit(limit),
        )

    def list_roles(
        self,
        context: AccessContext,
        *,
        cursor: str | None = None,
        limit: int = DEFAULT_PAGE_LIMIT,
    ) -> AdministrationPage:
        require_permission(context, PermissionCode.ROLES_ASSIGN)
        return self.repository.list_administration_roles(
            context.organization_id,
            cursor=_validated_cursor(cursor),
            limit=_validated_limit(limit),
        )

    def list_facilities(
        self,
        context: AccessContext,
        *,
        cursor: str | None = None,
        limit: int = DEFAULT_PAGE_LIMIT,
    ) -> AdministrationPage:
        _require_any_permission(
            context,
            PermissionCode.ROLES_ASSIGN,
            PermissionCode.IMPORTS_CREATE,
        )
        return self.repository.list_administration_facilities(
            context.organization_id,
            cursor=_validated_cursor(cursor),
            limit=_validated_limit(limit),
        )

    def issue_invitation(
        self,
        context: AccessContext,
        *,
        email: str,
        lifetime_hours: int,
        request_id: str | None,
        now: datetime | None = None,
    ) -> IssuedUserInvitation:
        require_permission(context, PermissionCode.USERS_MANAGE)
        if isinstance(lifetime_hours, bool) or not isinstance(lifetime_hours, int):
            raise AdministrationValidationError("lifetime_hours 必须是整数")
        if not 1 <= lifetime_hours <= 168:
            raise AdministrationValidationError("lifetime_hours 必须在 1 到 168 之间")

        from medical_ai.audit import AuditActor

        return self.invitation_service.issue_invitation(
            organization_id=context.organization_id,
            email=_validated_email(email),
            actor=AuditActor.from_membership_context(context),
            lifetime=timedelta(hours=lifetime_hours),
            request_id=request_id,
            now=_aware_utc_now(now),
            actor_identity_version=context.identity_version,
            actor_authorization_version=context.authorization_version,
            actor_session_id=context.session_id,
        )

    def replace_member_roles(
        self,
        context: AccessContext,
        *,
        membership_id: str,
        role_ids: object,
        expected_version: object,
        request_id: str | None,
        now: datetime | None = None,
    ) -> AdministrationMember:
        require_permission(context, PermissionCode.ROLES_ASSIGN)
        return self.repository.replace_membership_roles(
            context=context,
            membership_id=_validated_identifier(membership_id, "membership_id"),
            role_ids=_validated_identifier_list(role_ids, "role_ids", MAX_ROLE_ASSIGNMENTS),
            expected_version=_validated_version(expected_version),
            request_id=request_id,
            occurred_at=_naive_utc_now(now),
        )

    def replace_member_facility_scope(
        self,
        context: AccessContext,
        *,
        membership_id: str,
        facility_ids: object,
        expected_version: object,
        request_id: str | None,
        now: datetime | None = None,
    ) -> AdministrationMember:
        require_permission(context, PermissionCode.ROLES_ASSIGN)
        return self.repository.replace_membership_facility_scope(
            context=context,
            membership_id=_validated_identifier(membership_id, "membership_id"),
            facility_ids=_validated_identifier_list(
                facility_ids,
                "facility_ids",
                MAX_FACILITY_ASSIGNMENTS,
            ),
            expected_version=_validated_version(expected_version),
            request_id=request_id,
            occurred_at=_naive_utc_now(now),
        )

    def update_member_status(
        self,
        context: AccessContext,
        *,
        membership_id: str,
        status: object,
        expected_version: object,
        request_id: str | None,
        now: datetime | None = None,
    ) -> AdministrationMember:
        require_permission(context, PermissionCode.USERS_MANAGE)
        normalized_membership_id = _validated_identifier(membership_id, "membership_id")
        normalized_status = status.strip().casefold() if isinstance(status, str) else ""
        if normalized_status not in ALLOWED_MEMBERSHIP_STATUSES:
            raise AdministrationValidationError(
                "status 只能是 active 或 suspended"
            )
        if (
            normalized_membership_id == context.membership_id
            and normalized_status != "active"
        ):
            raise AdministrationSelfLockoutError
        return self.repository.update_membership_status(
            context=context,
            membership_id=normalized_membership_id,
            status=normalized_status,
            expected_version=_validated_version(expected_version),
            request_id=request_id,
            occurred_at=_naive_utc_now(now),
        )

    def sync_facilities(
        self,
        context: AccessContext,
        *,
        request_id: str | None,
        now: datetime | None = None,
    ) -> FacilityCatalogSyncResult:
        _require_any_permission(
            context,
            PermissionCode.IMPORTS_CREATE,
            PermissionCode.ROLES_ASSIGN,
        )
        return self.repository.sync_organization_facilities(
            context=context,
            request_id=request_id,
            occurred_at=_naive_utc_now(now),
        )


def _require_any_permission(context: AccessContext, *permissions: PermissionCode) -> None:
    if not any(context.has_permission(permission) for permission in permissions):
        raise PermissionDeniedError("|".join(permission.value for permission in permissions))


def _validated_limit(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_PAGE_LIMIT:
        raise AdministrationValidationError(f"limit 必须在 1 到 {MAX_PAGE_LIMIT} 之间")
    return value


def _validated_cursor(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 36:
        raise AdministrationValidationError("cursor 格式不正确")
    return value.strip()


def _validated_identifier(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 36:
        raise AdministrationValidationError(f"{field} 格式不正确")
    return value.strip()


def _validated_email(value: object) -> str:
    normalized = normalize_ascii_email(value)
    if normalized is None:
        raise AdministrationValidationError("email 格式不正确")
    return normalized


def _validated_identifier_list(value: object, field: str, maximum: int) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise AdministrationValidationError(f"{field} 必须是数组")
    if len(value) > maximum:
        raise AdministrationValidationError(f"{field} 最多包含 {maximum} 项")
    normalized = tuple(_validated_identifier(item, field) for item in value)
    if len(set(normalized)) != len(normalized):
        raise AdministrationValidationError(f"{field} 不能包含重复项")
    return normalized


def _validated_version(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise AdministrationValidationError("expected_version 必须是正整数")
    return value


def _aware_utc_now(value: datetime | None) -> datetime:
    current = value or datetime.now(UTC)
    if current.tzinfo is None:
        return current.replace(tzinfo=UTC)
    return current.astimezone(UTC)


def _naive_utc_now(value: datetime | None) -> datetime:
    return _aware_utc_now(value).replace(tzinfo=None)
