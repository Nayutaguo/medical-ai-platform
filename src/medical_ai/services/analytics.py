from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from medical_ai.agent import MedicalDataAgent
from medical_ai.agent.llm_client import LLMClientError, LLMTimeoutError
from medical_ai.authorization import AccessContext, AuthorizationService, PermissionCode
from medical_ai.config import Settings, get_settings
from medical_ai.db import QueryResult, public_schema
from medical_ai.query import (
    ExecutableQuery,
    QuerySpec,
    QueryValidationError,
    build_privacy_safe_metadata,
    compile_query,
    compile_scoped_query,
    filter_minimum_group_rows,
    require_governed_distinct_field,
    require_governed_query,
    validate_query_spec,
)
from medical_ai.repositories import MedicalDataRepository
from medical_ai.services.errors import ServiceUnavailableError, UpstreamServiceError, UpstreamTimeoutError
from medical_ai.services.policies import require_aggregate_query

MEDICAL_ANALYSIS_DISCLAIMER = "分析结果仅供数据统计与辅助洞察参考，不构成诊断、治疗或处方建议。"

AUTHORIZED_AGENT_PERMISSIONS = (
    PermissionCode.ANALYTICS_AGENT_EXECUTE,
    PermissionCode.ANALYTICS_SCHEMA_READ,
    PermissionCode.ANALYTICS_DISTINCT_READ,
    PermissionCode.ANALYTICS_QUERY_EXECUTE,
)


@dataclass(frozen=True)
class ServiceResult:
    """Transport-neutral service output and response metadata."""

    data: dict[str, Any]
    dimensions: list[str] = field(default_factory=list)
    metrics: list[str] = field(default_factory=list)
    query_time_ms: float | None = None


class AnalyticsService:
    """Orchestrate schema, aggregate query, and constrained Agent workflows."""

    def __init__(
        self,
        settings: Settings | None = None,
        repository: MedicalDataRepository | None = None,
        authorization_service: AuthorizationService | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.repository = repository or MedicalDataRepository(settings=self.settings)
        self.authorization_service = authorization_service or AuthorizationService()

    def health(self) -> ServiceResult:
        """Return the backward-compatible, dependency-free liveness result."""

        return self.liveness()

    def liveness(self) -> ServiceResult:
        """Report process liveness without contacting external dependencies."""

        return ServiceResult(data={"status": "alive"})

    def readiness(self) -> ServiceResult:
        """Report readiness after a bounded, table-independent MySQL probe."""

        result = self.repository.check_connection()
        return ServiceResult(
            data={"status": "ready"},
            query_time_ms=result.query_time_ms,
        )

    def schema(self) -> ServiceResult:
        return ServiceResult(data=public_schema())

    def schema_authorized(self, context: AccessContext) -> ServiceResult:
        """Return the allowlisted schema after an exact permission check."""

        self.authorization_service.require_permission(
            context,
            PermissionCode.ANALYTICS_SCHEMA_READ,
        )
        return self.schema()

    def distinct_values(self, table: str, field: str, limit: int) -> ServiceResult:
        result = self.repository.fetch_distinct_values(table, field, limit)
        values = [row[field] for row in result.rows]
        return ServiceResult(
            data={
                "table": table,
                "field": field,
                "values": values,
                "row_count": len(values),
                "truncated": result.truncated,
                "query_time_ms": result.query_time_ms,
            },
            dimensions=[field],
            query_time_ms=result.query_time_ms,
        )

    def distinct_values_authorized(
        self,
        table: str,
        field: str,
        limit: int,
        context: AccessContext,
    ) -> ServiceResult:
        """Read distinct values through a facility-scoped compiled query."""

        trusted_facility_ids = self.authorization_service.authorize_facility_scope(
            context,
            PermissionCode.ANALYTICS_DISTINCT_READ,
        )
        require_governed_distinct_field(table, field)
        effective_limit = min(
            max(limit, 1),
            max(self.settings.query_max_distinct_values, 1),
            1000,
        )
        spec = validate_query_spec(
            {
                "table": table,
                "select": [field],
                "filters": [{"field": field, "op": "is_not_null"}],
                "group_by": [field],
                "order_by": [{"field": field, "direction": "asc"}],
                "limit": effective_limit,
            }
        )
        compiled = compile_scoped_query(
            spec,
            trusted_facility_ids=trusted_facility_ids,
            privacy_min_group_size=self.settings.privacy_min_group_size,
        )
        result = self._apply_minimum_group_privacy(
            self.repository.execute(compiled),
            safe_limit=compiled.limit,
        )
        values = [row[field] for row in result.rows]
        return ServiceResult(
            data={
                "table": table,
                "field": field,
                "values": values,
                "row_count": len(values),
                "truncated": result.truncated,
                "query_time_ms": result.query_time_ms,
                "metadata": result.metadata,
            },
            dimensions=[field],
            query_time_ms=result.query_time_ms,
        )

    def query(self, query_spec: dict[str, Any] | QuerySpec) -> ServiceResult:
        spec = validate_query_spec(query_spec)
        require_aggregate_query(spec)
        compiled = compile_query(spec)
        return self._execute_query(spec, compiled)

    def query_authorized(
        self,
        query_spec: dict[str, Any] | QuerySpec,
        context: AccessContext,
    ) -> ServiceResult:
        """Execute an aggregate query inside an authenticated facility scope.

        The permission check is exact: administrative permissions do not imply
        analytical data access. The effective facility identifiers come only
        from the authenticated ``AccessContext`` and are injected separately
        from the user/LLM-controlled ``QuerySpec``.
        """

        spec = validate_query_spec(query_spec)
        require_aggregate_query(spec)
        trusted_facility_ids = self.authorization_service.authorize_facility_scope(
            context,
            PermissionCode.ANALYTICS_QUERY_EXECUTE,
        )
        require_governed_query(spec)
        compiled = compile_scoped_query(
            spec,
            trusted_facility_ids=trusted_facility_ids,
            privacy_min_group_size=self.settings.privacy_min_group_size,
        )
        return self._execute_privacy_query(spec, compiled)

    def _execute_query(
        self,
        spec: QuerySpec,
        compiled: ExecutableQuery,
    ) -> ServiceResult:
        """Execute one compiled query and build transport-neutral metadata."""

        result = self.repository.execute(compiled)
        return ServiceResult(
            data={
                "result": result.to_dict(),
                "compiled_sql": compiled.sql,
                "compiled_params": compiled.params,
            },
            dimensions=list(spec.group_by or spec.select),
            metrics=[metric.alias for metric in spec.metrics],
            query_time_ms=result.query_time_ms,
        )

    def _execute_privacy_query(
        self,
        spec: QuerySpec,
        compiled: ExecutableQuery,
    ) -> ServiceResult:
        """Execute and sanitize a minimum-group query before public output."""

        result = self._apply_minimum_group_privacy(
            self.repository.execute(compiled),
            safe_limit=compiled.limit,
        )
        return ServiceResult(
            data={"result": result.to_dict()},
            dimensions=list(spec.group_by or spec.select),
            metrics=[metric.alias for metric in spec.metrics],
            query_time_ms=result.query_time_ms,
        )

    def _apply_minimum_group_privacy(
        self,
        result: QueryResult,
        *,
        safe_limit: int,
    ) -> QueryResult:
        """Remove small groups and all internal privacy fields."""

        filtered = filter_minimum_group_rows(
            result.rows,
            result.columns,
            self.settings.privacy_min_group_size,
        )
        return QueryResult(
            columns=filtered.columns,
            rows=filtered.rows,
            row_count=len(filtered.rows),
            query_time_ms=result.query_time_ms,
            truncated=len(filtered.rows) >= safe_limit,
            metadata=build_privacy_safe_metadata(
                result.metadata,
                self.settings.privacy_min_group_size,
            ),
        )

    def ask(self, question: str, execute: bool = True) -> ServiceResult:
        """Run the legacy Agent path without an authenticated data scope."""

        return self._ask(question, execute=execute)

    def ask_authorized(
        self,
        question: str,
        context: AccessContext,
        execute: bool = True,
    ) -> ServiceResult:
        """Run the Agent after checking every capability exposed by its tools.

        The first production policy is deliberately conservative: entering the
        Agent requires the Agent, schema, distinct, and aggregate-query
        permissions because the selected route is decided only after planning.
        Possessing the Agent permission alone must never grant indirect data
        access through one of those tools.
        """

        for permission in AUTHORIZED_AGENT_PERMISSIONS:
            self.authorization_service.require_permission(context, permission)
        trusted_facility_ids = self.authorization_service.resolve_facility_scope(context)
        return self._ask(
            question,
            execute=execute,
            trusted_facility_ids=trusted_facility_ids,
        )

    def _ask(
        self,
        question: str,
        *,
        execute: bool,
        trusted_facility_ids: frozenset[str] | None = None,
    ) -> ServiceResult:
        """Execute an Agent request with an optional server-trusted scope."""

        if not self._llm_configured():
            raise ServiceUnavailableError("自然语言分析服务尚未配置")

        agent_kwargs: dict[str, Any] = {
            "executor": self.repository.executor,
            "require_aggregate": True,
        }
        if trusted_facility_ids is not None:
            agent_kwargs["trusted_facility_ids"] = trusted_facility_ids
            agent_kwargs["privacy_min_group_size"] = (
                self.settings.privacy_min_group_size
            )

        try:
            run = MedicalDataAgent(**agent_kwargs).run(
                question,
                execute=execute,
                interpret=execute,
            )
        except QueryValidationError as exc:
            raise QueryValidationError("模型生成的分析计划未通过安全校验") from exc
        except LLMTimeoutError as exc:
            raise UpstreamTimeoutError("上游模型服务响应超时，请稍后重试") from exc
        except LLMClientError as exc:
            raise UpstreamServiceError("上游模型服务返回异常，请稍后重试") from exc
        except RuntimeError as exc:
            raise UpstreamServiceError("上游模型服务调用失败") from exc

        payload = run.to_dict()
        payload.pop("raw_plan_response", None)
        payload.pop("raw_insight_response", None)
        payload["disclaimer"] = MEDICAL_ANALYSIS_DISCLAIMER

        spec = run.plan.query_spec
        return ServiceResult(
            data=payload,
            dimensions=list(spec.group_by or spec.select) if spec else [],
            metrics=[metric.alias for metric in spec.metrics] if spec else [],
            query_time_ms=run.result.query_time_ms if run.result else None,
        )

    def _llm_configured(self) -> bool:
        return bool(self.settings.llm_base_url and self.settings.llm_api_key and self.settings.llm_model)
