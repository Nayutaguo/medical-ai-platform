"""Structured query model, validation, and compilation."""

from medical_ai.query.capabilities import (
    INPATIENT_FIELD_CAPABILITIES,
    CardinalityClass,
    FieldCapability,
    SensitivityLevel,
    governed_field_capability,
    require_governed_distinct_field,
    require_governed_query,
)
from medical_ai.query.compiler import (
    ExecutableQuery,
    PrivacyFilteredRows,
    QueryCompiler,
    build_privacy_safe_metadata,
    compile_query,
    compile_scoped_query,
    filter_minimum_group_rows,
)
from medical_ai.query.models import (
    PRIVACY_GROUP_COUNT_ALIAS,
    PRIVACY_RESERVED_ALIAS_PREFIX,
    FilterSpec,
    MetricSpec,
    OrderBySpec,
    QuerySpec,
)
from medical_ai.query.validator import QueryValidationError, QueryValidator, validate_query_spec

__all__ = [
    "ExecutableQuery",
    "FieldCapability",
    "FilterSpec",
    "MetricSpec",
    "INPATIENT_FIELD_CAPABILITIES",
    "OrderBySpec",
    "PRIVACY_GROUP_COUNT_ALIAS",
    "PRIVACY_RESERVED_ALIAS_PREFIX",
    "PrivacyFilteredRows",
    "QueryCompiler",
    "QuerySpec",
    "QueryValidationError",
    "QueryValidator",
    "CardinalityClass",
    "SensitivityLevel",
    "build_privacy_safe_metadata",
    "compile_query",
    "compile_scoped_query",
    "filter_minimum_group_rows",
    "governed_field_capability",
    "require_governed_distinct_field",
    "require_governed_query",
    "validate_query_spec",
]
