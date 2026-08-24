"""Stable domain errors raised by authorization policy checks."""

from __future__ import annotations


class AuthorizationError(RuntimeError):
    """Base class for authorization failures exposed to application adapters."""

    error_code = "authorization_error"

    def __init__(self, message: str) -> None:
        super().__init__(message)


class PermissionDeniedError(AuthorizationError):
    """Raised when an access context lacks one explicit permission."""

    error_code = "permission_denied"

    def __init__(self, permission: str) -> None:
        self.permission = permission
        super().__init__(f"Permission is required: {permission}")


class OrganizationScopeDeniedError(AuthorizationError):
    """Raised when a request tries to cross its organization boundary."""

    error_code = "organization_scope_denied"

    def __init__(self) -> None:
        super().__init__("Requested organization is outside the authorized scope")


class EmptyFacilityScopeError(AuthorizationError):
    """Raised when facility scope resolution leaves no authorized facility."""

    error_code = "empty_facility_scope"

    def __init__(self) -> None:
        super().__init__("No authorized facility remains in the requested scope")
