from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Select, asc, desc, func, or_, select
from sqlalchemy.dialects import mysql

from medical_ai.db.schema import build_sqlalchemy_table, column_names
from medical_ai.query.models import (
    Aggregation,
    FilterOperator,
    PRIVACY_GROUP_COUNT_ALIAS,
    QuerySpec,
    SortDirection,
)
from medical_ai.query.validator import QueryValidationError, validate_query_spec

TRUSTED_FACILITY_FIELD = "PermanentFacilityId"
TRUSTED_FACILITY_CHUNK_SIZE = 1000
PRIVACY_SAFE_METADATA_KEYS = frozenset(
    {
        "table",
        "limit",
        "query_timeout_ms",
        "mysql_timeout_hint_set",
        "mysql_session_timeout_set",
    }
)


@dataclass(frozen=True)
class ExecutableQuery:
    statement: Select[Any]
    sql: str
    params: dict[str, Any]
    table: str
    limit: int


@dataclass(frozen=True)
class PrivacyFilteredRows:
    """Aggregate rows after the internal minimum-group guard is removed."""

    columns: list[str]
    rows: list[dict[str, Any]]


class QueryCompiler:
    def compile(
        self,
        spec: QuerySpec,
        *,
        trusted_facility_ids: Iterable[str] | None = None,
        privacy_min_group_size: int | None = None,
    ) -> ExecutableQuery:
        """Compile a validated query, optionally constrained by a trusted scope.

        ``trusted_facility_ids`` is deliberately an out-of-band compiler input:
        it is not part of ``QuerySpec`` and therefore cannot be supplied by a
        request or an LLM-generated plan. ``None`` preserves the legacy,
        unrestricted compiler path; any explicitly supplied scope must be
        non-empty.
        """

        spec = validate_query_spec(spec)
        trusted_scope = (
            None
            if trusted_facility_ids is None
            else _normalize_trusted_facility_ids(trusted_facility_ids)
        )
        privacy_threshold = (
            None
            if privacy_min_group_size is None
            else _validate_privacy_min_group_size(privacy_min_group_size)
        )
        if privacy_threshold is not None and not (spec.metrics or spec.group_by):
            raise QueryValidationError(
                "minimum-group privacy requires an aggregate or grouped query"
            )
        table = build_sqlalchemy_table(spec.table)
        columns = table.c

        metric_expressions: dict[str, Any] = {}

        if spec.metrics:
            dimension_fields = spec.group_by or spec.select
            select_expressions = [columns[field] for field in dimension_fields]

            for metric in spec.metrics:
                expression = self._metric_expression(metric.agg, metric.field, columns).label(metric.alias)
                metric_expressions[metric.alias] = expression
                select_expressions.append(expression)

            if privacy_threshold is not None:
                select_expressions.append(
                    func.count().label(PRIVACY_GROUP_COUNT_ALIAS)
                )

            statement = select(*select_expressions).select_from(table)
            if dimension_fields:
                statement = statement.group_by(*(columns[field] for field in dimension_fields))
        else:
            selected_fields = spec.select or spec.group_by or column_names(spec.table)
            select_expressions = [columns[field] for field in selected_fields]
            if privacy_threshold is not None:
                select_expressions.append(
                    func.count().label(PRIVACY_GROUP_COUNT_ALIAS)
                )
            statement = select(*select_expressions).select_from(table)
            if spec.group_by:
                statement = statement.group_by(*(columns[field] for field in spec.group_by))

        for filter_spec in spec.filters:
            statement = statement.where(self._filter_expression(filter_spec.op, columns[filter_spec.field], filter_spec.value))

        if trusted_scope is not None:
            statement = statement.where(
                self._trusted_facility_expression(
                    columns[TRUSTED_FACILITY_FIELD],
                    trusted_scope,
                )
            )

        if privacy_threshold is not None:
            statement = statement.having(func.count() >= privacy_threshold)

        for order_spec in spec.order_by:
            expression = metric_expressions.get(order_spec.field, columns.get(order_spec.field))
            if expression is None:
                raise ValueError(f"Cannot order by unknown expression {order_spec.field!r}")
            statement = statement.order_by(desc(expression) if order_spec.direction is SortDirection.DESC else asc(expression))

        statement = statement.limit(spec.limit)
        sql, params = compile_statement(statement)
        return ExecutableQuery(statement=statement, sql=sql, params=params, table=spec.table, limit=spec.limit)

    def _metric_expression(self, agg: Aggregation, field: str, columns: Any) -> Any:
        if agg is Aggregation.COUNT and field == "*":
            return func.count()

        column = columns[field]
        if agg is Aggregation.COUNT:
            return func.count(column)
        if agg is Aggregation.SUM:
            return func.sum(column)
        if agg is Aggregation.AVG:
            return func.avg(column)
        if agg is Aggregation.MIN:
            return func.min(column)
        if agg is Aggregation.MAX:
            return func.max(column)
        raise ValueError(f"Unsupported aggregation {agg!r}")

    def _filter_expression(self, op: FilterOperator, column: Any, value: Any) -> Any:
        if op is FilterOperator.EQ:
            return column == value
        if op is FilterOperator.NE:
            return column != value
        if op is FilterOperator.GT:
            return column > value
        if op is FilterOperator.GTE:
            return column >= value
        if op is FilterOperator.LT:
            return column < value
        if op is FilterOperator.LTE:
            return column <= value
        if op is FilterOperator.IN:
            return column.in_(list(value))
        if op is FilterOperator.NOT_IN:
            return column.not_in(list(value))
        if op is FilterOperator.BETWEEN:
            low, high = value
            return column.between(low, high)
        if op is FilterOperator.LIKE:
            return column.like(value)
        if op is FilterOperator.IS_NULL:
            return column.is_(None)
        if op is FilterOperator.IS_NOT_NULL:
            return column.is_not(None)
        raise ValueError(f"Unsupported operator {op!r}")

    def _trusted_facility_expression(
        self,
        column: Any,
        facility_ids: tuple[str, ...],
    ) -> Any:
        """Build a bound, chunked predicate for a server-trusted scope."""

        predicates = [
            column.in_(facility_ids[index : index + TRUSTED_FACILITY_CHUNK_SIZE])
            for index in range(0, len(facility_ids), TRUSTED_FACILITY_CHUNK_SIZE)
        ]
        return predicates[0] if len(predicates) == 1 else or_(*predicates)


def compile_statement(statement: Select[Any]) -> tuple[str, dict[str, Any]]:
    compiled = statement.compile(
        dialect=mysql.dialect(paramstyle="pyformat"),
        compile_kwargs={"render_postcompile": True},
    )
    return str(compiled), dict(compiled.params)


def compile_query(data: QuerySpec | dict[str, Any]) -> ExecutableQuery:
    spec = validate_query_spec(data)
    return QueryCompiler().compile(spec)


def compile_scoped_query(
    data: QuerySpec | dict[str, Any],
    *,
    trusted_facility_ids: Iterable[str] | None,
    privacy_min_group_size: int | None = None,
) -> ExecutableQuery:
    """Compile a query with a non-empty facility scope from trusted context."""

    spec = validate_query_spec(data)
    trusted_scope = _normalize_trusted_facility_ids(trusted_facility_ids)
    return QueryCompiler().compile(
        spec,
        trusted_facility_ids=trusted_scope,
        privacy_min_group_size=privacy_min_group_size,
    )


def filter_minimum_group_rows(
    rows: list[dict[str, Any]],
    columns: list[str],
    min_group_size: int,
) -> PrivacyFilteredRows:
    """Suppress small aggregate groups and remove the internal count column."""

    threshold = _validate_privacy_min_group_size(min_group_size)
    if PRIVACY_GROUP_COUNT_ALIAS not in columns:
        raise RuntimeError("privacy aggregate result is missing its internal count column")

    public_columns = [
        column for column in columns if column != PRIVACY_GROUP_COUNT_ALIAS
    ]
    public_rows: list[dict[str, Any]] = []
    for row in rows:
        if PRIVACY_GROUP_COUNT_ALIAS not in row:
            raise RuntimeError("privacy aggregate row is missing its internal count value")
        raw_group_count = row[PRIVACY_GROUP_COUNT_ALIAS]
        try:
            group_count = int(raw_group_count)
        except (TypeError, ValueError, OverflowError) as exc:
            raise RuntimeError("privacy aggregate row has an invalid internal count") from exc
        if group_count < 0 or group_count != raw_group_count:
            raise RuntimeError("privacy aggregate row has an invalid internal count")

        if group_count < threshold:
            continue
        public_rows.append(
            {
                key: value
                for key, value in row.items()
                if key != PRIVACY_GROUP_COUNT_ALIAS
            }
        )

    return PrivacyFilteredRows(
        columns=public_columns,
        rows=public_rows,
    )


def build_privacy_safe_metadata(
    metadata: dict[str, Any],
    min_group_size: int,
) -> dict[str, Any]:
    """Keep only non-row-derived metadata for a privacy-protected result."""

    threshold = _validate_privacy_min_group_size(min_group_size)
    public_metadata = {
        key: value
        for key, value in metadata.items()
        if key in PRIVACY_SAFE_METADATA_KEYS
    }
    public_metadata.update(
        {
            "privacy_applied": True,
            "min_group_size": threshold,
        }
    )
    return public_metadata


def _normalize_trusted_facility_ids(
    facility_ids: Iterable[str] | None,
) -> tuple[str, ...]:
    """Normalize trusted identifiers without applying the user-input list cap."""

    if facility_ids is None:
        raise QueryValidationError("trusted facility scope must not be empty")
    if isinstance(facility_ids, (str, bytes, bytearray)):
        raise QueryValidationError("trusted facility scope must be a non-empty collection")

    normalized: set[str] = set()
    for facility_id in facility_ids:
        if not isinstance(facility_id, str) or not facility_id.strip():
            raise QueryValidationError("trusted facility scope contains an invalid facility identifier")
        normalized.add(facility_id.strip())

    if not normalized:
        raise QueryValidationError("trusted facility scope must not be empty")
    return tuple(sorted(normalized))


def _validate_privacy_min_group_size(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 2 <= value <= 100:
        raise QueryValidationError(
            "privacy_min_group_size must be an integer between 2 and 100"
        )
    return value
