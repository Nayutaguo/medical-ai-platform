"""Transport-neutral models for query and Agent history."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class HistoryType(StrEnum):
    """The governed analytics workflow that produced a history entry."""

    QUERY = "query"
    AGENT = "agent"


@dataclass(frozen=True, slots=True)
class HistoryDraft:
    """Validated persistence input that deliberately contains no result rows."""

    history_type: HistoryType
    title: str
    question: str | None
    query_spec: dict[str, Any] | None = field(repr=False)
    chart_spec: dict[str, Any] | None = field(repr=False)
    row_count: int
    truncated: bool
    query_time_ms: float


@dataclass(frozen=True, slots=True)
class HistoryEntry:
    """One analysis-history entry belonging to an exact membership context."""

    id: int
    history_type: HistoryType
    title: str
    question: str | None
    query_spec: dict[str, Any] | None = field(repr=False)
    chart_spec: dict[str, Any] | None = field(repr=False)
    row_count: int
    truncated: bool
    query_time_ms: float
    is_favorite: bool
    created_at: datetime
    updated_at: datetime
    version: int

    def to_public_dict(self) -> dict[str, Any]:
        """Return the stable browser payload without tenant keys or result rows."""

        return {
            # MySQL BIGINT exceeds JavaScript's safe integer range; keep the
            # cursor/resource identifier exact by serializing it as text.
            "id": str(self.id),
            "kind": self.history_type.value,
            "title": self.title,
            "question": self.question,
            "query_spec": self.query_spec,
            "chart_spec": self.chart_spec,
            "row_count": self.row_count,
            "truncated": self.truncated,
            "query_time_ms": self.query_time_ms,
            "is_favorite": self.is_favorite,
            "created_at": _utc_isoformat(self.created_at),
            "updated_at": _utc_isoformat(self.updated_at),
            "version": self.version,
        }


@dataclass(frozen=True, slots=True)
class HistoryPage:
    """A descending, opaque-cursor page of history entries."""

    items: tuple[HistoryEntry, ...]
    next_cursor: str | None

    def to_public_dict(self) -> dict[str, Any]:
        """Return the stable list payload consumed by the browser client."""

        return {
            "items": [item.to_public_dict() for item in self.items],
            "next_cursor": self.next_cursor,
        }


def _utc_isoformat(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    else:
        value = value.astimezone(UTC)
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")
