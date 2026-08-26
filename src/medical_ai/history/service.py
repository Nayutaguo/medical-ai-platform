"""Application policy for durable, privacy-bounded analysis history."""

from __future__ import annotations

from collections.abc import Mapping
from math import isfinite
from typing import Any, Protocol

from medical_ai.agent.charting import ChartSpec
from medical_ai.authorization import AccessContext, PermissionCode
from medical_ai.authorization.errors import PermissionDeniedError
from medical_ai.history.errors import HistoryValidationError
from medical_ai.history.models import HistoryDraft, HistoryEntry, HistoryPage, HistoryType
from medical_ai.query import QuerySpec, QueryValidationError, validate_query_spec


class HistoryRepository(Protocol):
    """Persistence operations required by :class:`HistoryService`."""

    def create(self, context: AccessContext, draft: HistoryDraft) -> HistoryEntry:
        """Persist one sanitized history draft in the trusted context."""

        ...

    def list_page(
        self,
        context: AccessContext,
        *,
        allowed_types: frozenset[HistoryType],
        cursor: int | None,
        limit: int,
        favorite: bool | None,
    ) -> HistoryPage:
        """Return one membership-scoped page."""

        ...

    def set_favorite(
        self,
        context: AccessContext,
        history_id: int,
        *,
        is_favorite: bool,
        expected_version: int,
        allowed_types: frozenset[HistoryType],
    ) -> HistoryEntry:
        """Apply one optimistic favorite mutation."""

        ...

    def delete(
        self,
        context: AccessContext,
        history_id: int,
        *,
        allowed_types: frozenset[HistoryType],
    ) -> None:
        """Delete one visible entry."""

        ...


class HistoryService:
    """Validate persisted specs and enforce analytics permissions per entry type."""

    def __init__(self, repository: HistoryRepository) -> None:
        self._repository = repository

    def record_query(
        self,
        context: AccessContext,
        query_spec: Mapping[str, Any] | QuerySpec,
        *,
        result_data: Mapping[str, Any],
        query_time_ms: float | None,
    ) -> HistoryEntry:
        """Persist a successful governed query without copying its result rows."""

        _require_type_permission(context, HistoryType.QUERY)
        try:
            spec = validate_query_spec(query_spec)
        except QueryValidationError as exc:
            raise HistoryValidationError("query_spec is not valid") from exc
        result_summary = _result_summary(result_data, query_time_ms)
        draft = HistoryDraft(
            history_type=HistoryType.QUERY,
            title=_query_title(spec),
            question=None,
            query_spec=spec.model_dump(mode="json"),
            chart_spec=None,
            row_count=result_summary[0],
            truncated=result_summary[1],
            query_time_ms=result_summary[2],
        )
        return self._repository.create(context, draft)

    def record_agent(
        self,
        context: AccessContext,
        question: str,
        *,
        result_data: Mapping[str, Any],
        query_time_ms: float | None,
    ) -> HistoryEntry:
        """Persist a successful Agent plan, chart and numeric summary only."""

        _require_type_permission(context, HistoryType.AGENT)
        # Validate the request shape but do not persist the original natural-
        # language text. It may contain an accidentally entered identifier;
        # the validated QuerySpec is sufficient for safe replay.
        _normalized_text(question, "question", 2000)
        query_spec = _optional_validated_query_spec(result_data.get("query_spec"))
        chart_spec = _optional_validated_chart_spec(result_data.get("chart_spec"))
        result_summary = _result_summary(result_data, query_time_ms)
        title = _agent_title(query_spec)
        draft = HistoryDraft(
            history_type=HistoryType.AGENT,
            title=title,
            question=None,
            query_spec=query_spec,
            chart_spec=chart_spec,
            row_count=result_summary[0],
            truncated=result_summary[1],
            query_time_ms=result_summary[2],
        )
        return self._repository.create(context, draft)

    def list_history(
        self,
        context: AccessContext,
        *,
        cursor: int | None,
        limit: int,
        favorite: bool | None,
    ) -> HistoryPage:
        """Return only entry types covered by the current analytics grants."""

        if cursor is not None and (
            isinstance(cursor, bool) or cursor < 1 or cursor > 9_223_372_036_854_775_807
        ):
            raise HistoryValidationError("cursor must be a positive integer")
        if isinstance(limit, bool) or limit < 1 or limit > 100:
            raise HistoryValidationError("limit must be between 1 and 100")
        if favorite is not None and not isinstance(favorite, bool):
            raise HistoryValidationError("favorite must be true or false")
        return self._repository.list_page(
            context,
            allowed_types=_allowed_types(context),
            cursor=cursor,
            limit=limit,
            favorite=favorite,
        )

    def set_favorite(
        self,
        context: AccessContext,
        history_id: int,
        *,
        is_favorite: bool,
        expected_version: int,
    ) -> HistoryEntry:
        """Mutate favorite state using optimistic locking."""

        _positive_integer(history_id, "history_id")
        if not isinstance(is_favorite, bool):
            raise HistoryValidationError("is_favorite must be a boolean")
        _positive_integer(expected_version, "version")
        return self._repository.set_favorite(
            context,
            history_id,
            is_favorite=is_favorite,
            expected_version=expected_version,
            allowed_types=_allowed_types(context),
        )

    def delete_history(self, context: AccessContext, history_id: int) -> None:
        """Delete a visible history entry in the exact current context."""

        _positive_integer(history_id, "history_id")
        self._repository.delete(
            context,
            history_id,
            allowed_types=_allowed_types(context),
        )


def _allowed_types(context: AccessContext) -> frozenset[HistoryType]:
    allowed: set[HistoryType] = set()
    if context.has_permission(PermissionCode.ANALYTICS_QUERY_EXECUTE):
        allowed.add(HistoryType.QUERY)
    if context.has_permission(PermissionCode.ANALYTICS_AGENT_EXECUTE):
        allowed.add(HistoryType.AGENT)
    if not allowed:
        raise PermissionDeniedError(PermissionCode.ANALYTICS_QUERY_EXECUTE.value)
    return frozenset(allowed)


def _require_type_permission(context: AccessContext, history_type: HistoryType) -> None:
    permission = {
        HistoryType.QUERY: PermissionCode.ANALYTICS_QUERY_EXECUTE,
        HistoryType.AGENT: PermissionCode.ANALYTICS_AGENT_EXECUTE,
    }[history_type]
    if not context.has_permission(permission):
        raise PermissionDeniedError(permission.value)


def _optional_validated_query_spec(value: object) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise HistoryValidationError("Agent query_spec is not an object")
    try:
        return validate_query_spec(value).model_dump(mode="json")
    except QueryValidationError as exc:
        raise HistoryValidationError("Agent query_spec is not valid") from exc


def _optional_validated_chart_spec(value: object) -> dict[str, Any] | None:
    if value is None:
        return None
    try:
        spec = ChartSpec.model_validate(value)
    except (TypeError, ValueError) as exc:
        raise HistoryValidationError("Agent chart_spec is not valid") from exc
    # Model-authored title/reason text can echo a user's original question.
    # Persist only the declarative shape and replace free text with stable copy.
    return {
        **spec.model_dump(mode="json"),
        "title": "结果图表",
        "reason": "受控查询结果的声明式图表配置",
    }


def _result_summary(
    result_data: Mapping[str, Any],
    query_time_ms: float | None,
) -> tuple[int, bool, float]:
    """Extract only numeric summary fields and intentionally ignore ``rows``."""

    result = result_data.get("result")
    tool_result = result_data.get("tool_result")
    summary = result if isinstance(result, Mapping) else tool_result
    row_count_value = summary.get("row_count", 0) if isinstance(summary, Mapping) else 0
    if isinstance(row_count_value, bool) or not isinstance(row_count_value, int):
        raise HistoryValidationError("row_count must be a non-negative integer")
    if row_count_value < 0:
        raise HistoryValidationError("row_count must be a non-negative integer")

    truncated_value = summary.get("truncated", False) if isinstance(summary, Mapping) else False
    if not isinstance(truncated_value, bool):
        raise HistoryValidationError("truncated must be a boolean")

    duration_value: object = query_time_ms
    if duration_value is None and isinstance(summary, Mapping):
        duration_value = summary.get("query_time_ms", 0)
    if duration_value is None:
        duration_value = 0
    if isinstance(duration_value, bool) or not isinstance(duration_value, (int, float)):
        raise HistoryValidationError("query_time_ms must be a non-negative number")
    duration = float(duration_value)
    if duration < 0 or not isfinite(duration):
        raise HistoryValidationError("query_time_ms must be a non-negative number")
    if duration > 999_999_999.999:
        raise HistoryValidationError("query_time_ms exceeds its storage limit")
    return row_count_value, truncated_value, duration


def _query_title(spec: QuerySpec) -> str:
    dimensions = list(spec.group_by or spec.select)
    metrics = [metric.alias for metric in spec.metrics]
    pieces = [", ".join(values) for values in (dimensions, metrics) if values]
    return (" · ".join(pieces) or f"{spec.table} 数据查询")[:200]


def _agent_title(query_spec: Mapping[str, Any] | None) -> str:
    if query_spec is None:
        return "AI 分析"
    try:
        spec = validate_query_spec(query_spec)
    except QueryValidationError as exc:  # pragma: no cover - already validated above
        raise HistoryValidationError("Agent query_spec is not valid") from exc
    return f"AI 分析 · {_query_title(spec)}"[:200]


def _normalized_text(value: object, field_name: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise HistoryValidationError(f"{field_name} must be a non-empty string")
    return value.strip()[:limit]


def _positive_integer(value: object, field_name: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 1
        or value > 9_223_372_036_854_775_807
    ):
        raise HistoryValidationError(f"{field_name} must be a positive integer")
    return value
