"""Application boundary for safe append-only audit recording."""

from __future__ import annotations

from typing import Protocol

from medical_ai.audit.models import AuditEvent, AuditEventDraft


class AuditWriter(Protocol):
    """Append-only persistence port implemented by audit infrastructure."""

    def append(self, event: AuditEvent) -> None:
        """Persist exactly one validated event without mutating prior events."""

        ...


class AuditService:
    """Validate audit drafts before passing them to durable storage."""

    def __init__(self, writer: AuditWriter) -> None:
        self._writer = writer

    def record(self, draft: AuditEventDraft) -> AuditEvent:
        """Validate, append, and return the immutable persisted event shape."""

        event = AuditEvent.from_draft(draft)
        self._writer.append(event)
        return event
