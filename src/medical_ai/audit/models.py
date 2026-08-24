"""Immutable, transport-neutral security-audit models and validation."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol, TypeAlias

from medical_ai.audit.errors import (
    INVALID_DETAIL_VALUE,
    INVALID_ERROR_CODE,
    INVALID_EVENT_FIELD,
    SENSITIVE_DETAIL_KEY,
    UNSUPPORTED_DETAIL_KEY,
    AuditValidationError,
)


AuditDetailValue: TypeAlias = str | int | tuple[str, ...]

_DETAIL_LIST_LIMIT = 16
_DETAIL_STRING_LIMIT = 160
_COUNT_LIMIT = 2_147_483_647
_DURATION_LIMIT_MS = 86_400_000
_DETAIL_KEYS = frozenset(
    {
        "dimensions",
        "metrics",
        "row_count",
        "duration_ms",
        "job_type",
        "role_keys",
        "facility_count",
    }
)
_LIST_DETAIL_KEYS = frozenset({"dimensions", "metrics", "role_keys"})
_COUNT_DETAIL_KEYS = frozenset({"row_count", "facility_count"})
_SENSITIVE_KEY_PARTS = (
    "prompt",
    "question",
    "result",
    "rows",
    "sql",
    "password",
    "token",
    "cookie",
    "header",
    "connection",
    "patient",
    "medical_record",
    "person_name",
    "diagnosis",
    "procedure",
    "address",
    "phone",
    "email",
)
_SYMBOLIC_VALUE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]*$")
_ACTION = re.compile(r"^[a-z][a-z0-9_.:-]*$")
_RESOURCE_TYPE = re.compile(r"^[a-z][a-z0-9_.:-]*$")
_ERROR_CODE = re.compile(r"^[A-Z][A-Z0-9_]*$")


class AuditOutcome(StrEnum):
    """Stable outcome vocabulary persisted by the audit subsystem."""

    SUCCESS = "success"
    DENIED = "denied"
    FAILURE = "failure"


class AuditActorKind(StrEnum):
    """Provenance vocabulary enforced by both the domain and database."""

    USER = "user"
    SYSTEM = "system"


class MembershipActorContext(Protocol):
    """Trusted authenticated membership facts accepted by the audit layer."""

    membership_id: str
    user_id: str
    organization_id: str


@dataclass(frozen=True, slots=True)
class AuditActor:
    """Validated actor provenance for one audit event.

    User actors should be built from the authenticated ``AccessContext`` via
    :meth:`from_membership_context`. The composite database foreign key remains
    the final authority and rejects any stale or cross-organization context.
    System actors cannot carry a user or membership identifier; they may be
    either organization-scoped or global.
    """

    kind: AuditActorKind
    organization_id: str | None
    membership_id: str | None
    user_id: str | None

    def __post_init__(self) -> None:
        try:
            kind = AuditActorKind(self.kind)
        except (TypeError, ValueError) as exc:
            raise AuditValidationError(
                INVALID_EVENT_FIELD,
                "audit actor kind is not part of the stable vocabulary",
            ) from exc
        object.__setattr__(self, "kind", kind)

        organization_id = _optional_text(
            self.organization_id,
            field_name="organization_id",
            limit=36,
        )
        membership_id = _optional_text(
            self.membership_id,
            field_name="actor_membership_id",
            limit=36,
        )
        user_id = _optional_text(
            self.user_id,
            field_name="actor_user_id",
            limit=36,
        )
        object.__setattr__(self, "organization_id", organization_id)
        object.__setattr__(self, "membership_id", membership_id)
        object.__setattr__(self, "user_id", user_id)

        if kind is AuditActorKind.USER:
            if organization_id is None or membership_id is None or user_id is None:
                raise AuditValidationError(
                    INVALID_EVENT_FIELD,
                    "user audit actors require a complete membership context",
                )
        elif membership_id is not None or user_id is not None:
            raise AuditValidationError(
                INVALID_EVENT_FIELD,
                "system audit actors cannot carry user membership identifiers",
            )

    @classmethod
    def from_membership_context(cls, context: MembershipActorContext) -> AuditActor:
        """Copy actor facts from an authenticated membership context."""

        return cls(
            kind=AuditActorKind.USER,
            organization_id=context.organization_id,
            membership_id=context.membership_id,
            user_id=context.user_id,
        )

    @classmethod
    def system(cls, organization_id: str | None = None) -> AuditActor:
        """Build a system actor without pretending to be an application user."""

        return cls(
            kind=AuditActorKind.SYSTEM,
            organization_id=organization_id,
            membership_id=None,
            user_id=None,
        )


@dataclass(frozen=True, slots=True)
class AuditEventDraft:
    """Untrusted audit input awaiting validation and normalization.

    ``details`` is excluded from ``repr`` because a caller can construct a
    draft before validation.  This prevents accidental secret disclosure even
    when a rejected draft is included in a debug log.
    """

    request_id: str | None
    actor: AuditActor
    action: str
    resource_type: str
    resource_id: str | None
    outcome: AuditOutcome
    error_code: str | None = None
    details: Mapping[str, object] | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class AuditEvent:
    """Validated append-only audit event ready for persistence."""

    request_id: str | None
    actor: AuditActor
    action: str
    resource_type: str
    resource_id: str | None
    outcome: AuditOutcome
    error_code: str | None
    details: Mapping[str, AuditDetailValue] = field(repr=False)

    def __post_init__(self) -> None:
        """Enforce invariants even when infrastructure constructs an event directly."""

        object.__setattr__(
            self,
            "request_id",
            _optional_text(self.request_id, field_name="request_id", limit=128),
        )
        if not isinstance(self.actor, AuditActor):
            raise AuditValidationError(
                INVALID_EVENT_FIELD,
                "audit actor must come from a validated actor context",
            )
        object.__setattr__(
            self,
            "action",
            _symbolic_text(
                self.action,
                field_name="action",
                limit=120,
                pattern=_ACTION,
            ),
        )
        object.__setattr__(
            self,
            "resource_type",
            _symbolic_text(
                self.resource_type,
                field_name="resource_type",
                limit=80,
                pattern=_RESOURCE_TYPE,
            ),
        )
        object.__setattr__(
            self,
            "resource_id",
            _optional_text(self.resource_id, field_name="resource_id", limit=128),
        )
        try:
            outcome = AuditOutcome(self.outcome)
        except (TypeError, ValueError) as exc:
            raise AuditValidationError(
                INVALID_EVENT_FIELD,
                "audit outcome is not part of the stable vocabulary",
            ) from exc
        object.__setattr__(self, "outcome", outcome)
        object.__setattr__(
            self,
            "error_code",
            _validate_error_code(self.error_code, outcome=outcome),
        )
        object.__setattr__(
            self,
            "details",
            MappingProxyType(sanitize_audit_details(self.details)),
        )

    @classmethod
    def from_draft(cls, draft: AuditEventDraft) -> AuditEvent:
        """Validate a draft and copy only bounded aggregate metadata."""

        request_id = _optional_text(draft.request_id, field_name="request_id", limit=128)
        if not isinstance(draft.actor, AuditActor):
            raise AuditValidationError(
                INVALID_EVENT_FIELD,
                "audit actor must come from a validated actor context",
            )
        action = _symbolic_text(
            draft.action,
            field_name="action",
            limit=120,
            pattern=_ACTION,
        )
        resource_type = _symbolic_text(
            draft.resource_type,
            field_name="resource_type",
            limit=80,
            pattern=_RESOURCE_TYPE,
        )
        resource_id = _optional_text(draft.resource_id, field_name="resource_id", limit=128)

        try:
            outcome = AuditOutcome(draft.outcome)
        except (TypeError, ValueError) as exc:
            raise AuditValidationError(
                INVALID_EVENT_FIELD,
                "audit outcome is not part of the stable vocabulary",
            ) from exc

        error_code = _validate_error_code(draft.error_code, outcome=outcome)
        details = MappingProxyType(sanitize_audit_details(draft.details))
        return cls(
            request_id=request_id,
            actor=draft.actor,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            outcome=outcome,
            error_code=error_code,
            details=details,
        )

    @property
    def actor_kind(self) -> AuditActorKind:
        """Return the normalized actor provenance."""

        return self.actor.kind

    @property
    def organization_id(self) -> str | None:
        """Return the trusted tenant snapshot, when the event is tenant-scoped."""

        return self.actor.organization_id

    @property
    def actor_membership_id(self) -> str | None:
        """Return the trusted membership snapshot for a user actor."""

        return self.actor.membership_id

    @property
    def actor_user_id(self) -> str | None:
        """Return the trusted user snapshot for a user actor."""

        return self.actor.user_id


def sanitize_audit_details(
    details: Mapping[str, object] | None,
) -> dict[str, AuditDetailValue]:
    """Return bounded JSON metadata or reject unsafe/unknown fields.

    The allowlist contains only aggregate query shape, counts, durations, job
    categories, and role identifiers.  Nested mappings and arbitrary result
    values are deliberately unsupported, so patient-level rows cannot enter
    the event through this channel.
    """

    if details is None:
        return {}
    if not isinstance(details, Mapping):
        raise AuditValidationError(
            INVALID_DETAIL_VALUE,
            "audit details must be a mapping",
        )
    if len(details) > len(_DETAIL_KEYS):
        raise AuditValidationError(
            INVALID_DETAIL_VALUE,
            "audit details contain too many fields",
        )

    normalized: dict[str, AuditDetailValue] = {}
    for key, value in details.items():
        if not isinstance(key, str):
            raise AuditValidationError(
                UNSUPPORTED_DETAIL_KEY,
                "audit detail key is not allowlisted",
            )
        if key not in _DETAIL_KEYS:
            folded_key = key.casefold().replace("-", "_")
            if any(part in folded_key for part in _SENSITIVE_KEY_PARTS):
                raise AuditValidationError(
                    SENSITIVE_DETAIL_KEY,
                    "sensitive audit detail keys are forbidden",
                )
            raise AuditValidationError(
                UNSUPPORTED_DETAIL_KEY,
                "audit detail key is not allowlisted",
            )

        if key in _LIST_DETAIL_KEYS:
            normalized[key] = _normalize_symbol_list(value)
        elif key in _COUNT_DETAIL_KEYS:
            normalized[key] = _bounded_integer(value, maximum=_COUNT_LIMIT)
        elif key == "duration_ms":
            normalized[key] = _bounded_integer(value, maximum=_DURATION_LIMIT_MS)
        else:
            normalized[key] = _symbolic_text(
                value,
                field_name="audit detail",
                limit=80,
                pattern=_SYMBOLIC_VALUE,
                error_code=INVALID_DETAIL_VALUE,
            )
    return normalized


def _normalize_symbol_list(value: object) -> tuple[str, ...]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise AuditValidationError(
            INVALID_DETAIL_VALUE,
            "audit detail list has an invalid value",
        )
    if len(value) > _DETAIL_LIST_LIMIT:
        raise AuditValidationError(
            INVALID_DETAIL_VALUE,
            "audit detail list exceeds its item limit",
        )
    return tuple(
        _symbolic_text(
            item,
            field_name="audit detail item",
            limit=_DETAIL_STRING_LIMIT,
            pattern=_SYMBOLIC_VALUE,
            error_code=INVALID_DETAIL_VALUE,
        )
        for item in value
    )


def _bounded_integer(value: object, *, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        raise AuditValidationError(
            INVALID_DETAIL_VALUE,
            "audit numeric detail is outside its allowed range",
        )
    return value


def _symbolic_text(
    value: object,
    *,
    field_name: str,
    limit: int,
    pattern: re.Pattern[str],
    error_code: str = INVALID_EVENT_FIELD,
) -> str:
    if not isinstance(value, str):
        raise AuditValidationError(error_code, f"{field_name} has an invalid value")
    normalized = value.strip()
    if not normalized or len(normalized) > limit or pattern.fullmatch(normalized) is None:
        raise AuditValidationError(error_code, f"{field_name} has an invalid value")
    return normalized


def _optional_text(value: object, *, field_name: str, limit: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise AuditValidationError(INVALID_EVENT_FIELD, f"{field_name} has an invalid value")
    normalized = value.strip()
    if not normalized or len(normalized) > limit or any(ord(char) < 32 for char in normalized):
        raise AuditValidationError(INVALID_EVENT_FIELD, f"{field_name} has an invalid value")
    return normalized


def _validate_error_code(error_code: object, *, outcome: AuditOutcome) -> str | None:
    if outcome is AuditOutcome.SUCCESS:
        if error_code is not None:
            raise AuditValidationError(
                INVALID_ERROR_CODE,
                "successful audit events cannot carry an error code",
            )
        return None
    if not isinstance(error_code, str):
        raise AuditValidationError(
            INVALID_ERROR_CODE,
            "unsuccessful audit events require a stable error code",
        )
    normalized = error_code.strip()
    if not normalized or len(normalized) > 80 or _ERROR_CODE.fullmatch(normalized) is None:
        raise AuditValidationError(
            INVALID_ERROR_CODE,
            "audit error code must be a stable symbolic code",
        )
    return normalized
