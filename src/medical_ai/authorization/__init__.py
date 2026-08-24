"""Public authorization domain API."""

from medical_ai.authorization.errors import (
    AuthorizationError,
    EmptyFacilityScopeError,
    OrganizationScopeDeniedError,
    PermissionDeniedError,
)
from medical_ai.authorization.models import AccessContext, PermissionCode
from medical_ai.authorization.service import (
    AuthorizationService,
    authorize_facility_scope,
    require_organization_scope,
    require_permission,
    resolve_facility_scope,
)

__all__ = [
    "AccessContext",
    "AuthorizationError",
    "AuthorizationService",
    "EmptyFacilityScopeError",
    "OrganizationScopeDeniedError",
    "PermissionCode",
    "PermissionDeniedError",
    "authorize_facility_scope",
    "require_organization_scope",
    "require_permission",
    "resolve_facility_scope",
]
