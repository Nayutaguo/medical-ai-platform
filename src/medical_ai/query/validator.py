import re
from collections.abc import Mapping, Sequence
from decimal import Decimal
from math import isfinite
from typing import Any

from pydantic import ValidationError

from medical_ai.db.schema import get_column_spec, get_table_spec
from medical_ai.query.models import (
    Aggregation,
    FilterOperator,
    PRIVACY_RESERVED_ALIAS_PREFIX,
    QuerySpec,
)

ALIAS_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
LIST_OPERATORS = {FilterOperator.IN, FilterOperator.NOT_IN}
RANGE_OPERATORS = {FilterOperator.BETWEEN}
UNARY_OPERATORS = {FilterOperator.IS_NULL, FilterOperator.IS_NOT_NULL}
SCALAR_OPERATORS = {
    FilterOperator.EQ,
    FilterOperator.NE,
    FilterOperator.GT,
    FilterOperator.GTE,
    FilterOperator.LT,
    FilterOperator.LTE,
    FilterOperator.LIKE,
}
NUMERIC_AGGREGATIONS = {Aggregation.SUM, Aggregation.AVG}
MAX_FILTER_STRING_BYTES = 512
MAX_LIKE_PATTERN_BYTES = 160


class QueryValidationError(ValueError):
    """Raised when a QuerySpec is syntactically valid JSON but not allowed."""


class QueryValidator:
    def validate(self, spec: QuerySpec) -> QuerySpec:
        try:
            table = get_table_spec(spec.table)
        except KeyError as exc:
            raise QueryValidationError(str(exc)) from exc

        self._ensure_unique(spec.select, "select")
        self._ensure_unique(spec.group_by, "group_by")

        for field in [*spec.select, *spec.group_by]:
            self._require_field(spec.table, field)

        if spec.metrics and spec.select and not set(spec.select).issubset(spec.group_by):
            raise QueryValidationError("select fields must also appear in group_by when metrics are used")

        if spec.group_by and spec.select and not set(spec.select).issubset(spec.group_by):
            raise QueryValidationError("select fields must be a subset of group_by when group_by is used")

        aliases: set[str] = set()
        for metric in spec.metrics:
            if not ALIAS_PATTERN.fullmatch(metric.alias):
                raise QueryValidationError(f"Invalid metric alias {metric.alias!r}")
            if metric.alias.lower().startswith(PRIVACY_RESERVED_ALIAS_PREFIX):
                raise QueryValidationError(
                    f"Metric alias prefix {PRIVACY_RESERVED_ALIAS_PREFIX!r} is reserved"
                )
            if metric.alias in aliases:
                raise QueryValidationError(f"Duplicate metric alias {metric.alias!r}")
            if metric.alias in table.columns:
                raise QueryValidationError(f"Metric alias {metric.alias!r} conflicts with a table field")
            aliases.add(metric.alias)
            self._validate_metric(spec.table, metric.field, metric.agg)

        for filter_spec in spec.filters:
            column = self._require_field(spec.table, filter_spec.field)
            if filter_spec.op.value not in column.allowed_ops:
                raise QueryValidationError(
                    f"Operator {filter_spec.op.value!r} is not allowed for field {filter_spec.field!r}"
                )
            self._validate_filter_value(
                filter_spec.op,
                filter_spec.value,
                filter_spec.field,
                data_type=column.data_type,
            )

        orderable_fields = set(aliases)
        if spec.metrics:
            orderable_fields.update(spec.group_by)
            orderable_fields.update(spec.select)
        else:
            orderable_fields.update(table.columns)

        for order_spec in spec.order_by:
            if order_spec.field not in orderable_fields:
                allowed = ", ".join(sorted(orderable_fields))
                raise QueryValidationError(f"Cannot order by {order_spec.field!r}. Allowed order fields: {allowed}")

        return spec

    def _require_field(self, table_name: str, field: str):
        try:
            return get_column_spec(table_name, field)
        except KeyError as exc:
            raise QueryValidationError(str(exc)) from exc

    def _validate_metric(self, table_name: str, field: str, agg: Aggregation) -> None:
        if field == "*":
            if agg is not Aggregation.COUNT:
                raise QueryValidationError("Only count aggregation can use field '*'")
            return

        column = self._require_field(table_name, field)
        if agg in NUMERIC_AGGREGATIONS and not column.numeric:
            raise QueryValidationError(f"Aggregation {agg.value!r} requires a numeric field")

    def _validate_filter_value(
        self,
        op: FilterOperator,
        value: Any,
        field: str,
        *,
        data_type: str,
    ) -> None:
        if op in UNARY_OPERATORS:
            if value is not None:
                raise QueryValidationError(f"Operator {op.value!r} for field {field!r} must not provide a value")
            return

        if op in LIST_OPERATORS:
            if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
                raise QueryValidationError(f"Operator {op.value!r} for field {field!r} requires a list value")
            if len(value) == 0:
                raise QueryValidationError(f"Operator {op.value!r} for field {field!r} requires a non-empty list")
            if len(value) > 1000:
                raise QueryValidationError(f"Operator {op.value!r} for field {field!r} allows at most 1000 values")
            for item in value:
                self._validate_scalar_value(item, field, data_type=data_type)
            return

        if op in RANGE_OPERATORS:
            if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)) or len(value) != 2:
                raise QueryValidationError(f"Operator {op.value!r} for field {field!r} requires exactly two values")
            for item in value:
                self._validate_scalar_value(item, field, data_type=data_type)
            if value[0] > value[1]:
                raise QueryValidationError(
                    f"Operator {op.value!r} for field {field!r} requires an ascending range"
                )
            return

        if op is FilterOperator.LIKE:
            if not isinstance(value, str):
                raise QueryValidationError(f"Operator {op.value!r} for field {field!r} requires a string value")
            encoded_length = len(value.encode("utf-8"))
            if encoded_length == 0 or encoded_length > MAX_LIKE_PATTERN_BYTES:
                raise QueryValidationError(
                    f"Operator {op.value!r} for field {field!r} has an invalid pattern length"
                )
            if not value.replace("%", "").replace("_", ""):
                raise QueryValidationError(
                    f"Operator {op.value!r} for field {field!r} requires a selective pattern"
                )
            return

        if op in SCALAR_OPERATORS:
            if value is None or isinstance(value, (Mapping, Sequence)) and not isinstance(value, (str, bytes, bytearray)):
                raise QueryValidationError(f"Operator {op.value!r} for field {field!r} requires a scalar value")
            self._validate_scalar_value(value, field, data_type=data_type)

    def _validate_scalar_value(
        self,
        value: Any,
        field: str,
        *,
        data_type: str,
    ) -> None:
        """Reject nested, oversized, non-finite, and type-confused bind values."""

        if value is None or isinstance(value, (bool, Mapping, Sequence)) and not isinstance(value, str):
            raise QueryValidationError(f"Filter value for field {field!r} has an invalid scalar type")

        if data_type == "integer":
            if not isinstance(value, int) or isinstance(value, bool):
                raise QueryValidationError(f"Filter value for field {field!r} must be an integer")
            return

        if data_type == "decimal":
            if not isinstance(value, (int, float, Decimal)) or isinstance(value, bool):
                raise QueryValidationError(f"Filter value for field {field!r} must be numeric")
            if isinstance(value, float) and not isfinite(value):
                raise QueryValidationError(f"Filter value for field {field!r} must be finite")
            if isinstance(value, Decimal) and not value.is_finite():
                raise QueryValidationError(f"Filter value for field {field!r} must be finite")
            return

        if not isinstance(value, str):
            raise QueryValidationError(f"Filter value for field {field!r} must be a string")
        encoded_length = len(value.encode("utf-8"))
        if encoded_length == 0 or encoded_length > MAX_FILTER_STRING_BYTES:
            raise QueryValidationError(f"Filter value for field {field!r} exceeds its length limit")

    def _ensure_unique(self, values: list[str], label: str) -> None:
        duplicates = sorted({value for value in values if values.count(value) > 1})
        if duplicates:
            raise QueryValidationError(f"Duplicate fields in {label}: {', '.join(duplicates)}")


def validate_query_spec(data: Mapping[str, Any] | QuerySpec) -> QuerySpec:
    try:
        spec = data if isinstance(data, QuerySpec) else QuerySpec.model_validate(data)
    except ValidationError as exc:
        raise QueryValidationError(str(exc)) from exc
    return QueryValidator().validate(spec)
