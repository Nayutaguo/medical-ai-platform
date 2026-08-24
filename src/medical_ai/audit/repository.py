"""SQLAlchemy append-only writer for the control-plane audit ledger."""

from __future__ import annotations

from sqlalchemy.engine import Engine

from medical_ai.audit.models import AuditDetailValue, AuditEvent
from medical_ai.identity.schema import audit_events


class SqlAlchemyAuditWriter:
    """Insert validated events into ``identity.audit_events``.

    The writer intentionally exposes no update or delete operation.  Database
    retention and archival are separate administrative concerns and must not
    be reachable through the request-time audit interface.
    """

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def append(self, event: AuditEvent) -> None:
        """Execute one INSERT while leaving database-owned columns untouched."""

        statement = audit_events.insert().values(
            request_id=event.request_id,
            organization_id=event.organization_id,
            actor_kind=event.actor_kind.value,
            actor_membership_id=event.actor_membership_id,
            actor_user_id=event.actor_user_id,
            action=event.action,
            resource_type=event.resource_type,
            resource_id=event.resource_id,
            outcome=event.outcome.value,
            error_code=event.error_code,
            details=_json_details(event.details),
        )
        with self._engine.begin() as connection:
            connection.execute(statement)


def _json_details(details: object) -> dict[str, object]:
    """Convert the immutable domain representation into JSON-compatible data."""

    return {
        key: list(value) if isinstance(value, tuple) else value
        for key, value in dict(details).items()
    }
