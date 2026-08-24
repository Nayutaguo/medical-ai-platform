"""Pure authorization checks shared by HTTP, MCP, and background workers."""

from __future__ import annotations

from collections.abc import Iterable

from medical_ai.authorization.errors import (
    EmptyFacilityScopeError,
    OrganizationScopeDeniedError,
    PermissionDeniedError,
)
from medical_ai.authorization.models import AccessContext, PermissionCode


def require_permission(context: AccessContext, permission: PermissionCode) -> None:
    """Require one explicit permission without inferring role hierarchy.

    Administrative permissions deliberately do not imply analytical data-access
    permissions, and analytical permissions do not imply user administration.
    """

    required = PermissionCode(permission)
    if not context.has_permission(required):
        raise PermissionDeniedError(required.value)


def require_organization_scope(context: AccessContext, organization_id: str) -> None:
    """Reject attempts to use an access context for another organization."""

    if not isinstance(organization_id, str) or organization_id.strip() != context.organization_id:
        raise OrganizationScopeDeniedError


def resolve_facility_scope(
    context: AccessContext,
    requested_facility_ids: Iterable[str] | None = None,
    *,
    organization_id: str | None = None,
) -> frozenset[str]:
    """Intersect a requested facility set with the membership's allowed set.

    ``None`` means all facilities already authorized for the membership. An
    explicit request can only reduce that set: unauthorized facility identifiers
    are discarded and never expand the effective scope. A disjoint, empty, or
    otherwise unusable result is rejected rather than interpreted as unrestricted.
    """

    if organization_id is not None:
        require_organization_scope(context, organization_id)

    if requested_facility_ids is None:
        effective_scope = context.allowed_facility_ids
    else:
        requested_scope = _normalize_requested_facilities(requested_facility_ids)
        effective_scope = requested_scope & context.allowed_facility_ids

    if not effective_scope:
        raise EmptyFacilityScopeError
    return effective_scope


def authorize_facility_scope(
    context: AccessContext,
    permission: PermissionCode,
    requested_facility_ids: Iterable[str] | None = None,
    *,
    organization_id: str | None = None,
) -> frozenset[str]:
    """Require a permission and resolve the bounded facility scope atomically."""

    require_permission(context, permission)
    return resolve_facility_scope(
        context,
        requested_facility_ids,
        organization_id=organization_id,
    )


class AuthorizationService:
    """Stateless facade for dependency injection into application services."""

    def require_permission(self, context: AccessContext, permission: PermissionCode) -> None:
        """Delegate to :func:`require_permission`."""

        require_permission(context, permission)

    def resolve_facility_scope(
        self,
        context: AccessContext,
        requested_facility_ids: Iterable[str] | None = None,
        *,
        organization_id: str | None = None,
    ) -> frozenset[str]:
        """Delegate to :func:`resolve_facility_scope`."""

        return resolve_facility_scope(
            context,
            requested_facility_ids,
            organization_id=organization_id,
        )

    def authorize_facility_scope(
        self,
        context: AccessContext,
        permission: PermissionCode,
        requested_facility_ids: Iterable[str] | None = None,
        *,
        organization_id: str | None = None,
    ) -> frozenset[str]:
        """Require a permission and return its bounded facility scope."""

        return authorize_facility_scope(
            context,
            permission,
            requested_facility_ids,
            organization_id=organization_id,
        )


def _normalize_requested_facilities(facility_ids: Iterable[str]) -> frozenset[str]:
    normalized: set[str] = set()
    for facility_id in facility_ids:
        if not isinstance(facility_id, str) or not facility_id.strip():
            raise EmptyFacilityScopeError
        normalized.add(facility_id.strip())
    if not normalized:
        raise EmptyFacilityScopeError
    return frozenset(normalized)
