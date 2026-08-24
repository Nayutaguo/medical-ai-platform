from __future__ import annotations

from collections.abc import Mapping

import pytest

from medical_ai.audit import (
    AuditActor,
    AuditActorKind,
    AuditEvent,
    AuditEventDraft,
    AuditOutcome,
    AuditService,
    AuditValidationError,
)
from medical_ai.authorization import AccessContext


class RecordingWriter:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def append(self, event: AuditEvent) -> None:
        self.events.append(event)


def _draft(**overrides: object) -> AuditEventDraft:
    values: dict[str, object] = {
        "request_id": "request-123",
        "actor": AuditActor.from_membership_context(
            AccessContext(
                user_id="user-1",
                organization_id="organization-1",
                membership_id="membership-1",
                permissions=frozenset(),
                allowed_facility_ids=frozenset(),
                identity_version=1,
                authorization_version=1,
            )
        ),
        "action": "analytics.query",
        "resource_type": "dataset",
        "resource_id": "inpatient-v1",
        "outcome": AuditOutcome.SUCCESS,
        "details": {
            "dimensions": ["discharge_year", "AgeGroup"],
            "metrics": ["patient_count", "avg_total_charges"],
            "row_count": 2,
            "duration_ms": 18,
            "job_type": "interactive_query",
            "role_keys": ["analyst", "organization_admin"],
            "facility_count": 3,
        },
    }
    values.update(overrides)
    return AuditEventDraft(**values)  # type: ignore[arg-type]


def test_audit_service_normalizes_only_bounded_aggregate_metadata() -> None:
    writer = RecordingWriter()

    event = AuditService(writer).record(_draft())

    assert writer.events == [event]
    assert event.outcome is AuditOutcome.SUCCESS
    assert event.actor_kind is AuditActorKind.USER
    assert event.organization_id == "organization-1"
    assert event.actor_membership_id == "membership-1"
    assert event.actor_user_id == "user-1"
    assert event.details == {
        "dimensions": ("discharge_year", "AgeGroup"),
        "metrics": ("patient_count", "avg_total_charges"),
        "row_count": 2,
        "duration_ms": 18,
        "job_type": "interactive_query",
        "role_keys": ("analyst", "organization_admin"),
        "facility_count": 3,
    }
    assert isinstance(event.details, Mapping)
    with pytest.raises(TypeError):
        event.details["row_count"] = 99  # type: ignore[index]


@pytest.mark.parametrize(
    "key",
    [
        "prompt",
        "question",
        "result",
        "rows",
        "raw_sql",
        "password",
        "session_token",
        "cookie",
        "authorization_header",
        "connection_string",
        "patient_id",
        "diagnosis_description",
    ],
)
def test_sensitive_or_patient_level_detail_keys_are_stably_rejected(key: str) -> None:
    writer = RecordingWriter()

    with pytest.raises(AuditValidationError) as captured:
        AuditService(writer).record(_draft(details={key: "must-not-leak"}))

    assert captured.value.code == "AUDIT_DETAILS_SENSITIVE_KEY"
    assert "must-not-leak" not in str(captured.value)
    assert writer.events == []


def test_unknown_detail_key_is_rejected_instead_of_silently_removed() -> None:
    with pytest.raises(AuditValidationError) as captured:
        AuditEvent.from_draft(_draft(details={"custom_note": "not allowed"}))

    assert captured.value.code == "AUDIT_DETAILS_KEY_NOT_ALLOWED"


@pytest.mark.parametrize(
    "details",
    [
        {"dimensions": [["nested"]]},
        {"dimensions": [f"dimension_{index}" for index in range(17)]},
        {"metrics": ["m" * 161]},
        {"row_count": -1},
        {"row_count": True},
        {"duration_ms": 86_400_001},
        {"job_type": {"nested": "mapping"}},
    ],
)
def test_detail_values_enforce_nesting_length_count_and_range_limits(
    details: dict[str, object],
) -> None:
    with pytest.raises(AuditValidationError) as captured:
        AuditEvent.from_draft(_draft(details=details))

    assert captured.value.code == "AUDIT_DETAILS_VALUE_INVALID"


def test_failure_requires_a_stable_symbolic_error_code() -> None:
    failed = AuditEvent.from_draft(
        _draft(outcome=AuditOutcome.FAILURE, error_code="UPSTREAM_TIMEOUT")
    )
    assert failed.error_code == "UPSTREAM_TIMEOUT"

    with pytest.raises(AuditValidationError) as missing:
        AuditEvent.from_draft(_draft(outcome=AuditOutcome.DENIED, error_code=None))
    assert missing.value.code == "AUDIT_ERROR_CODE_INVALID"

    with pytest.raises(AuditValidationError) as prose:
        AuditEvent.from_draft(
            _draft(outcome=AuditOutcome.FAILURE, error_code="upstream timed out")
        )
    assert prose.value.code == "AUDIT_ERROR_CODE_INVALID"


def test_draft_and_event_repr_never_include_detail_values() -> None:
    draft = _draft(details={"job_type": "sensitive-looking-value"})
    assert "sensitive-looking-value" not in repr(draft)

    event = AuditEvent.from_draft(draft)
    assert "sensitive-looking-value" not in repr(event)
    assert "details" not in repr(event)


def test_direct_event_construction_cannot_bypass_detail_validation() -> None:
    with pytest.raises(AuditValidationError) as captured:
        AuditEvent(
            request_id="request-1",
            actor=AuditActor.system(),
            action="analytics.query",
            resource_type="dataset",
            resource_id=None,
            outcome=AuditOutcome.SUCCESS,
            error_code=None,
            details={"password": "must-not-enter-event"},
        )

    assert captured.value.code == "AUDIT_DETAILS_SENSITIVE_KEY"
    assert "must-not-enter-event" not in str(captured.value)


def test_actor_context_is_copied_from_authenticated_membership() -> None:
    context = AccessContext(
        user_id="user-9",
        organization_id="organization-7",
        membership_id="membership-8",
        permissions=frozenset(),
        allowed_facility_ids=frozenset(),
        identity_version=3,
        authorization_version=5,
    )

    actor = AuditActor.from_membership_context(context)

    assert actor == AuditActor(
        kind=AuditActorKind.USER,
        organization_id="organization-7",
        membership_id="membership-8",
        user_id="user-9",
    )


def test_system_actor_cannot_impersonate_a_user_or_membership() -> None:
    with pytest.raises(AuditValidationError) as captured:
        AuditActor(
            kind=AuditActorKind.SYSTEM,
            organization_id="organization-1",
            membership_id="membership-1",
            user_id="user-1",
        )

    assert captured.value.code == "AUDIT_EVENT_FIELD_INVALID"


def test_user_actor_requires_complete_membership_context() -> None:
    with pytest.raises(AuditValidationError) as captured:
        AuditActor(
            kind=AuditActorKind.USER,
            organization_id="organization-1",
            membership_id=None,
            user_id="user-1",
        )

    assert captured.value.code == "AUDIT_EVENT_FIELD_INVALID"
