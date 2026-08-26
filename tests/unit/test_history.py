from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import create_engine, event

from medical_ai.authorization import AccessContext, PermissionCode
from medical_ai.authorization.errors import PermissionDeniedError
from medical_ai.history import (
    HistoryDraft,
    HistoryEntry,
    HistoryNotFoundError,
    HistoryPage,
    HistoryService,
    HistoryType,
    HistoryVersionConflictError,
    SqlAlchemyHistoryRepository,
)


QUERY_SPEC = {
    "table": "inpatient",
    "group_by": ["AgeGroup"],
    "metrics": [{"field": "*", "agg": "count", "alias": "patient_count"}],
    "limit": 100,
}


class CapturingRepository:
    def __init__(self) -> None:
        self.drafts: list[HistoryDraft] = []
        self.list_arguments = None

    def create(self, context, draft):
        self.drafts.append(draft)
        return _entry(draft=draft)

    def list_page(self, context, **kwargs):
        self.list_arguments = (context, kwargs)
        return HistoryPage(items=(), next_cursor=None)

    def set_favorite(self, context, history_id, **kwargs):
        return _entry(is_favorite=kwargs["is_favorite"], version=2)

    def delete(self, context, history_id, **kwargs):
        return None


def test_query_history_persists_validated_spec_and_numeric_summary_without_rows() -> None:
    repository = CapturingRepository()
    service = HistoryService(repository)

    service.record_query(
        _context(PermissionCode.ANALYTICS_QUERY_EXECUTE),
        QUERY_SPEC,
        result_data={
            "result": {
                "rows": [{"patient_secret": "must-not-persist"}],
                "row_count": 7,
                "query_time_ms": 41.5,
            }
        },
        query_time_ms=42.25,
    )

    draft = repository.drafts[0]
    assert draft.history_type is HistoryType.QUERY
    assert draft.query_spec == {
        **QUERY_SPEC,
        "select": [],
        "filters": [],
        "order_by": [],
    }
    assert draft.row_count == 7
    assert draft.query_time_ms == 42.25
    assert draft.chart_spec is None
    assert draft.question is None
    assert not hasattr(draft, "rows")
    assert "must-not-persist" not in repr(draft)


def test_agent_history_validates_chart_and_restricts_visibility_by_permission() -> None:
    repository = CapturingRepository()
    service = HistoryService(repository)
    context = _context(PermissionCode.ANALYTICS_AGENT_EXECUTE)
    payload = {
        "analysis_goal": "按年龄组统计人数",
        "query_spec": QUERY_SPEC,
        "chart_spec": {
            "chart_type": "bar",
            "x_field": "AgeGroup",
            "y_field": "patient_count",
            "series_field": None,
            "title": "年龄组人数",
            "reason": "单维度聚合",
        },
        "result": {"rows": [{"patient_secret": "hidden"}], "row_count": 3},
    }

    service.record_agent(
        context,
        "按年龄组统计人数",
        result_data=payload,
        query_time_ms=None,
    )
    service.list_history(
        context,
        cursor=None,
        limit=20,
        favorite=None,
    )

    draft = repository.drafts[0]
    assert draft.history_type is HistoryType.AGENT
    assert draft.title == "AI 分析 · AgeGroup · patient_count"
    assert draft.question is None
    assert draft.chart_spec and draft.chart_spec["chart_type"] == "bar"
    assert draft.chart_spec["title"] == "结果图表"
    assert draft.chart_spec["reason"] == "受控查询结果的声明式图表配置"
    assert draft.row_count == 3
    assert draft.query_time_ms == 0
    assert repository.list_arguments[1]["allowed_types"] == frozenset(
        {HistoryType.AGENT}
    )
    assert "hidden" not in repr(draft)


def test_history_requires_existing_analytics_permissions() -> None:
    service = HistoryService(CapturingRepository())
    no_permissions = _context()

    with pytest.raises(PermissionDeniedError):
        service.list_history(
            no_permissions,
            cursor=None,
            limit=20,
            favorite=None,
        )
    with pytest.raises(PermissionDeniedError):
        service.record_query(
            no_permissions,
            QUERY_SPEC,
            result_data={"result": {"row_count": 0}},
            query_time_ms=0,
        )


def test_sqlalchemy_repository_enforces_full_context_cursor_and_optimistic_lock() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def register_utc_timestamp(dbapi_connection, _record) -> None:
        dbapi_connection.create_function(
            "UTC_TIMESTAMP",
            1,
            lambda _precision: "2026-08-26 12:00:00.000000",
        )

    with engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TABLE analysis_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                organization_id CHAR(36) NOT NULL,
                membership_id CHAR(36) NOT NULL,
                user_id CHAR(36) NOT NULL,
                history_type VARCHAR(16) NOT NULL,
                title VARCHAR(200) NOT NULL,
                question VARCHAR(2000),
                query_spec JSON,
                chart_spec JSON,
                row_count BIGINT NOT NULL DEFAULT 0,
                truncated BOOLEAN NOT NULL DEFAULT 0,
                query_time_ms NUMERIC(12,3) NOT NULL DEFAULT 0,
                is_favorite BOOLEAN NOT NULL DEFAULT 0,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                version INTEGER NOT NULL DEFAULT 1
            )
            """
        )

    repository = SqlAlchemyHistoryRepository(engine=engine)
    first_context = _context(
        PermissionCode.ANALYTICS_QUERY_EXECUTE,
        organization_id="organization-1",
        membership_id="membership-1",
    )
    other_context = _context(
        PermissionCode.ANALYTICS_QUERY_EXECUTE,
        organization_id="organization-2",
        membership_id="membership-2",
    )
    draft = HistoryDraft(
        history_type=HistoryType.QUERY,
        title="AgeGroup · patient_count",
        question=None,
        query_spec=QUERY_SPEC,
        chart_spec=None,
        row_count=2,
        truncated=False,
        query_time_ms=1.25,
    )
    first = repository.create(first_context, draft)
    second = repository.create(first_context, draft)
    repository.create(other_context, draft)

    first_page = repository.list_page(
        first_context,
        allowed_types=frozenset({HistoryType.QUERY}),
        cursor=None,
        limit=1,
        favorite=None,
    )
    assert [item.id for item in first_page.items] == [second.id]
    assert first_page.next_cursor == str(second.id)
    next_page = repository.list_page(
        first_context,
        allowed_types=frozenset({HistoryType.QUERY}),
        cursor=int(first_page.next_cursor),
        limit=10,
        favorite=None,
    )
    assert [item.id for item in next_page.items] == [first.id]

    with pytest.raises(HistoryNotFoundError):
        repository.set_favorite(
            other_context,
            first.id,
            is_favorite=True,
            expected_version=1,
            allowed_types=frozenset({HistoryType.QUERY}),
        )
    with pytest.raises(HistoryVersionConflictError):
        repository.set_favorite(
            first_context,
            first.id,
            is_favorite=True,
            expected_version=99,
            allowed_types=frozenset({HistoryType.QUERY}),
        )

    favorite = repository.set_favorite(
        first_context,
        first.id,
        is_favorite=True,
        expected_version=1,
        allowed_types=frozenset({HistoryType.QUERY}),
    )
    assert favorite.is_favorite is True
    assert favorite.version == 2
    assert repository.list_page(
        first_context,
        allowed_types=frozenset({HistoryType.QUERY}),
        cursor=None,
        limit=10,
        favorite=True,
    ).items == (favorite,)

    with pytest.raises(HistoryNotFoundError):
        repository.delete(
            other_context,
            first.id,
            allowed_types=frozenset({HistoryType.QUERY}),
        )
    repository.delete(
        first_context,
        first.id,
        allowed_types=frozenset({HistoryType.QUERY}),
    )


def _context(
    *permissions: PermissionCode,
    organization_id: str = "organization-1",
    membership_id: str = "membership-1",
) -> AccessContext:
    return AccessContext(
        user_id="user-1",
        organization_id=organization_id,
        membership_id=membership_id,
        permissions=frozenset(permissions),
        allowed_facility_ids=frozenset({"facility-1"}),
        identity_version=1,
        authorization_version=1,
    )


def _entry(
    *,
    draft: HistoryDraft | None = None,
    is_favorite: bool = False,
    version: int = 1,
) -> HistoryEntry:
    draft = draft or HistoryDraft(
        history_type=HistoryType.QUERY,
        title="Query",
        question=None,
        query_spec=QUERY_SPEC,
        chart_spec=None,
        row_count=0,
        truncated=False,
        query_time_ms=0,
    )
    now = datetime(2026, 8, 26, 12, 0)
    return HistoryEntry(
        id=1,
        history_type=draft.history_type,
        title=draft.title,
        question=draft.question,
        query_spec=draft.query_spec,
        chart_spec=draft.chart_spec,
        row_count=draft.row_count,
        truncated=draft.truncated,
        query_time_ms=draft.query_time_ms,
        is_favorite=is_favorite,
        created_at=now,
        updated_at=now,
        version=version,
    )
