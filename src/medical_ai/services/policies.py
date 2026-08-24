from __future__ import annotations

from medical_ai.query import QuerySpec, QueryValidationError


def require_aggregate_query(spec: QuerySpec) -> None:
    """Reject patient-level result shapes on every public analysis transport."""

    if not spec.metrics:
        raise QueryValidationError("公开分析接口仅允许返回聚合结果，query_spec.metrics 不能为空")
