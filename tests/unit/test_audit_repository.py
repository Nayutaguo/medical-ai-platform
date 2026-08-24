from __future__ import annotations

from typing import Any

from medical_ai.audit import (
    AuditActor,
    AuditEvent,
    AuditEventDraft,
    AuditOutcome,
    SqlAlchemyAuditWriter,
)
from medical_ai.identity.schema import audit_events


class CapturingConnection:
    def __init__(self) -> None:
        self.statements: list[Any] = []

    def execute(self, statement: Any) -> None:
        self.statements.append(statement)


class TransactionContext:
    def __init__(self, connection: CapturingConnection) -> None:
        self.connection = connection

    def __enter__(self) -> CapturingConnection:
        return self.connection

    def __exit__(self, *_args: object) -> None:
        return None


class CapturingEngine:
    def __init__(self) -> None:
        self.connection = CapturingConnection()
        self.begin_count = 0

    def begin(self) -> TransactionContext:
        self.begin_count += 1
        return TransactionContext(self.connection)


def test_sqlalchemy_writer_executes_exactly_one_insert() -> None:
    engine = CapturingEngine()
    writer = SqlAlchemyAuditWriter(engine)  # type: ignore[arg-type]
    event = AuditEvent.from_draft(
        AuditEventDraft(
            request_id="request-1",
            actor=AuditActor(
                kind="user",
                organization_id="organization-1",
                membership_id="membership-1",
                user_id="user-1",
            ),
            action="identity.login",
            resource_type="session",
            resource_id="session-1",
            outcome=AuditOutcome.DENIED,
            error_code="INVALID_CREDENTIALS",
            details={"duration_ms": 12, "role_keys": ["analyst"]},
        )
    )

    writer.append(event)

    assert engine.begin_count == 1
    assert len(engine.connection.statements) == 1
    statement = engine.connection.statements[0]
    assert statement.is_insert is True
    assert statement.table is audit_events
    assert statement.compile().params == {
        "request_id": "request-1",
        "organization_id": "organization-1",
        "actor_kind": "user",
        "actor_membership_id": "membership-1",
        "actor_user_id": "user-1",
        "action": "identity.login",
        "resource_type": "session",
        "resource_id": "session-1",
        "outcome": "denied",
        "error_code": "INVALID_CREDENTIALS",
        "details": {"duration_ms": 12, "role_keys": ["analyst"]},
    }
    assert not hasattr(writer, "update")
    assert not hasattr(writer, "delete")
