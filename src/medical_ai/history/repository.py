"""SQLAlchemy persistence for strictly membership-scoped analysis history."""

from __future__ import annotations

from collections.abc import Collection, Mapping
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, create_engine, delete, select, text, update
from sqlalchemy.engine import Engine, RowMapping

from medical_ai.authorization import AccessContext
from medical_ai.config import Settings, get_settings
from medical_ai.history.errors import (
    HistoryNotFoundError,
    HistoryVersionConflictError,
)
from medical_ai.history.models import HistoryDraft, HistoryEntry, HistoryPage, HistoryType
from medical_ai.identity.schema import analysis_history


class SqlAlchemyHistoryRepository:
    """Persist history while binding every operation to all trusted context keys."""

    def __init__(
        self,
        settings: Settings | None = None,
        engine: Engine | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.engine = engine or create_engine(
            self.settings.mysql_url(),
            pool_pre_ping=True,
            connect_args={"connect_timeout": 10},
        )

    def create(self, context: AccessContext, draft: HistoryDraft) -> HistoryEntry:
        """Insert one sanitized entry and reload it inside the same transaction."""

        statement = analysis_history.insert().values(
            organization_id=context.organization_id,
            membership_id=context.membership_id,
            user_id=context.user_id,
            history_type=draft.history_type.value,
            title=draft.title,
            question=draft.question,
            query_spec=draft.query_spec,
            chart_spec=draft.chart_spec,
            row_count=draft.row_count,
            truncated=draft.truncated,
            query_time_ms=draft.query_time_ms,
        )
        with self.engine.begin() as connection:
            result = connection.execute(statement)
            history_id = int(result.inserted_primary_key[0])
            row = connection.execute(
                _entry_select().where(
                    analysis_history.c.id == history_id,
                    *_context_conditions(context),
                )
            ).mappings().one()
        return _entry_from_row(row)

    def list_page(
        self,
        context: AccessContext,
        *,
        allowed_types: Collection[HistoryType],
        cursor: int | None,
        limit: int,
        favorite: bool | None,
    ) -> HistoryPage:
        """List a descending ID page without accepting caller-owned tenant keys."""

        conditions = [
            *_context_conditions(context),
            analysis_history.c.history_type.in_(_type_values(allowed_types)),
        ]
        if cursor is not None:
            conditions.append(analysis_history.c.id < cursor)
        if favorite is not None:
            conditions.append(analysis_history.c.is_favorite.is_(favorite))

        statement = (
            _entry_select()
            .where(and_(*conditions))
            .order_by(analysis_history.c.id.desc())
            .limit(limit + 1)
        )
        with self.engine.connect() as connection:
            rows = list(connection.execute(statement).mappings())

        has_more = len(rows) > limit
        visible_rows = rows[:limit]
        next_cursor = str(visible_rows[-1]["id"]) if has_more else None
        return HistoryPage(
            items=tuple(_entry_from_row(row) for row in visible_rows),
            next_cursor=next_cursor,
        )

    def set_favorite(
        self,
        context: AccessContext,
        history_id: int,
        *,
        is_favorite: bool,
        expected_version: int,
        allowed_types: Collection[HistoryType],
    ) -> HistoryEntry:
        """Update favorite state with tenant filters and optimistic locking."""

        scope = [
            analysis_history.c.id == history_id,
            *_context_conditions(context),
            analysis_history.c.history_type.in_(_type_values(allowed_types)),
        ]
        statement = (
            update(analysis_history)
            .where(*scope, analysis_history.c.version == expected_version)
            .values(
                is_favorite=is_favorite,
                updated_at=text("UTC_TIMESTAMP(6)"),
                version=analysis_history.c.version + 1,
            )
        )
        with self.engine.begin() as connection:
            result = connection.execute(statement)
            if result.rowcount != 1:
                existing_version = connection.execute(
                    select(analysis_history.c.version).where(*scope)
                ).scalar_one_or_none()
                if existing_version is None:
                    raise HistoryNotFoundError("history entry was not found")
                raise HistoryVersionConflictError(
                    "history entry changed; refresh before retrying"
                )
            row = connection.execute(
                _entry_select().where(*scope)
            ).mappings().one()
        return _entry_from_row(row)

    def delete(
        self,
        context: AccessContext,
        history_id: int,
        *,
        allowed_types: Collection[HistoryType],
    ) -> None:
        """Delete only an entry visible to the exact authenticated membership."""

        statement = delete(analysis_history).where(
            analysis_history.c.id == history_id,
            *_context_conditions(context),
            analysis_history.c.history_type.in_(_type_values(allowed_types)),
        )
        with self.engine.begin() as connection:
            result = connection.execute(statement)
        if result.rowcount != 1:
            raise HistoryNotFoundError("history entry was not found")


def _context_conditions(context: AccessContext) -> tuple[Any, ...]:
    return (
        analysis_history.c.organization_id == context.organization_id,
        analysis_history.c.membership_id == context.membership_id,
        analysis_history.c.user_id == context.user_id,
    )


def _type_values(allowed_types: Collection[HistoryType]) -> tuple[str, ...]:
    return tuple(sorted(item.value for item in allowed_types))


def _entry_select():
    return select(
        analysis_history.c.id,
        analysis_history.c.history_type,
        analysis_history.c.title,
        analysis_history.c.question,
        analysis_history.c.query_spec,
        analysis_history.c.chart_spec,
        analysis_history.c.row_count,
        analysis_history.c.truncated,
        analysis_history.c.query_time_ms,
        analysis_history.c.is_favorite,
        analysis_history.c.created_at,
        analysis_history.c.updated_at,
        analysis_history.c.version,
    )


def _entry_from_row(row: RowMapping | Mapping[str, Any]) -> HistoryEntry:
    query_time = row["query_time_ms"]
    if isinstance(query_time, Decimal):
        query_time = float(query_time)
    return HistoryEntry(
        id=int(row["id"]),
        history_type=HistoryType(row["history_type"]),
        title=str(row["title"]),
        question=str(row["question"]) if row["question"] is not None else None,
        query_spec=dict(row["query_spec"]) if row["query_spec"] is not None else None,
        chart_spec=dict(row["chart_spec"]) if row["chart_spec"] is not None else None,
        row_count=int(row["row_count"]),
        truncated=bool(row["truncated"]),
        query_time_ms=float(query_time),
        is_favorite=bool(row["is_favorite"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        version=int(row["version"]),
    )
