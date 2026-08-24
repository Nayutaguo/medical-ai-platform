"""Stable, transport-neutral errors raised by the audit boundary."""

from __future__ import annotations


class AuditValidationError(ValueError):
    """Reject unsafe audit metadata without echoing its value.

    ``code`` is suitable for metrics and internal error mapping.  The
    exception message is intentionally generic so rejected secrets cannot be
    copied into application logs by an outer exception handler.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


SENSITIVE_DETAIL_KEY = "AUDIT_DETAILS_SENSITIVE_KEY"
UNSUPPORTED_DETAIL_KEY = "AUDIT_DETAILS_KEY_NOT_ALLOWED"
INVALID_DETAIL_VALUE = "AUDIT_DETAILS_VALUE_INVALID"
INVALID_EVENT_FIELD = "AUDIT_EVENT_FIELD_INVALID"
INVALID_ERROR_CODE = "AUDIT_ERROR_CODE_INVALID"
