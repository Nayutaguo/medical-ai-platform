from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from medical_ai.agent.charting import ChartSpec, recommend_chart
from medical_ai.agent.intent import AgentIntent, AgentRoute, IntentType
from medical_ai.agent.llm_client import ChatMessage, LLMClientError, OpenAICompatibleClient
from medical_ai.agent.query_planner import extract_json_object, normalize_llm_queryspec
from medical_ai.db import MySQLExecutor, QueryResult, public_schema
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

LOGGER = logging.getLogger(__name__)


class _TrustedFacilityScopeUnset:
    """Sentinel distinguishing an omitted scope from an explicit empty scope."""


_TRUSTED_FACILITY_SCOPE_UNSET = _TrustedFacilityScopeUnset()


class ToolName(str, Enum):
    GET_DATABASE_SCHEMA = "get_database_schema"
    GET_DISTINCT_VALUES = "get_distinct_values"
    QUERY_MEDICAL_DATA = "query_medical_data"


class AgentPlan(BaseModel):
    intent: AgentIntent
    tool_name: ToolName
    tool_args: dict[str, Any] = Field(default_factory=dict)
    analysis_goal: str
    query_spec: QuerySpec | None = None
    chart_spec: ChartSpec | None = None
    assumptions: list[str] = Field(default_factory=list)
    execution_steps: list[str] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")


class AgentInsight(BaseModel):
    summary: str
    chart_reading: str
    observations: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    follow_up_questions: list[str] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")


class AgentWarning(BaseModel):
    code: str
    message: str

    model_config = ConfigDict(extra="forbid")


@dataclass(frozen=True)
class AgentRun:
    question: str
    plan: AgentPlan
    compiled_query: ExecutableQuery | None = None
    result: QueryResult | None = None
    tool_result: dict[str, Any] | None = None
    insight: AgentInsight | None = None
    raw_plan_response: str = ""
    raw_insight_response: str = ""
    warnings: list[AgentWarning] = field(default_factory=list)
    redact_compiled_query: bool = False

    def to_dict(self) -> dict[str, Any]:
        expose_compiled_query = self.compiled_query and not self.redact_compiled_query
        return {
            "question": self.question,
            "intent": self.plan.intent.model_dump(mode="json"),
            "tool_name": self.plan.tool_name.value,
            "tool_args": self.plan.tool_args,
            "analysis_goal": self.plan.analysis_goal,
            "assumptions": self.plan.assumptions,
            "execution_steps": self.plan.execution_steps,
            "query_spec": self.plan.query_spec.model_dump(mode="json") if self.plan.query_spec else None,
            "chart_spec": self.plan.chart_spec.model_dump(mode="json") if self.plan.chart_spec else None,
            "compiled_sql": self.compiled_query.sql if expose_compiled_query else None,
            "compiled_params": self.compiled_query.params if expose_compiled_query else {},
            "result": self.result.to_dict() if self.result else None,
            "tool_result": self.tool_result,
            "insight": self.insight.model_dump(mode="json") if self.insight else None,
            "warnings": [warning.model_dump(mode="json") for warning in self.warnings],
            "raw_plan_response": self.raw_plan_response,
            "raw_insight_response": self.raw_insight_response,
        }


class MedicalDataAgent:
    def __init__(
        self,
        client: OpenAICompatibleClient | None = None,
        executor: MySQLExecutor | None = None,
        require_aggregate: bool = False,
        trusted_facility_ids: (
            Iterable[str] | None | _TrustedFacilityScopeUnset
        ) = _TRUSTED_FACILITY_SCOPE_UNSET,
        privacy_min_group_size: int = 5,
    ) -> None:
        self.client = client or OpenAICompatibleClient()
        self.executor = executor or MySQLExecutor()
        self.require_aggregate = require_aggregate
        self.trusted_facility_ids = (
            None
            if isinstance(trusted_facility_ids, _TrustedFacilityScopeUnset)
            else _normalize_trusted_facility_scope(trusted_facility_ids)
        )
        self.privacy_min_group_size = _validate_privacy_min_group_size(
            privacy_min_group_size
        )

    def plan(self, question: str) -> tuple[AgentPlan, str]:
        response = self.client.chat_json(
            [
                ChatMessage(role="system", content=_planning_system_prompt()),
                ChatMessage(role="user", content=question),
            ],
            max_tokens=2200,
        )
        try:
            data = normalize_agent_plan(extract_json_object(response))
            plan = AgentPlan.model_validate(data)
        except (TypeError, ValueError) as exc:
            raise QueryValidationError(f"LLM produced invalid AgentPlan: {exc}") from exc

        if plan.tool_name is ToolName.QUERY_MEDICAL_DATA:
            if plan.query_spec is None:
                raise QueryValidationError(f"LLM selected query_medical_data without query_spec. Raw response: {response}")
            validated_spec = validate_query_spec(plan.query_spec)
            if self.require_aggregate and not validated_spec.metrics:
                raise QueryValidationError("公开分析接口仅允许返回聚合结果，query_spec.metrics 不能为空")
            if self.trusted_facility_ids is not None:
                require_governed_query(validated_spec)
            plan = plan.model_copy(update={"query_spec": validated_spec})

        return plan, response

    def run(self, question: str, execute: bool = True, interpret: bool = True) -> AgentRun:
        plan, raw_plan = self.plan(question)

        if plan.tool_name is ToolName.GET_DATABASE_SCHEMA:
            return AgentRun(
                question=question,
                plan=plan,
                tool_result=public_schema(),
                raw_plan_response=raw_plan,
            )

        if plan.tool_name is ToolName.GET_DISTINCT_VALUES:
            tool_result = self._run_distinct_tool(plan.tool_args)
            return AgentRun(
                question=question,
                plan=plan,
                tool_result=tool_result,
                raw_plan_response=raw_plan,
            )

        compiled_query = self._compile_query(plan.query_spec)
        if not execute:
            chart_spec = plan.chart_spec or recommend_chart(plan.query_spec, None, plan.intent)
            return AgentRun(
                question=question,
                plan=plan.model_copy(update={"chart_spec": chart_spec}),
                compiled_query=compiled_query,
                raw_plan_response=raw_plan,
                redact_compiled_query=self.trusted_facility_ids is not None,
            )

        result = self.executor.execute(compiled_query)
        if self.trusted_facility_ids is not None:
            result = self._apply_minimum_group_privacy(
                result,
                safe_limit=compiled_query.limit,
            )
        chart_spec = _validated_or_recommended_chart(plan, result)
        plan = plan.model_copy(update={"chart_spec": chart_spec})

        insight: AgentInsight | None = None
        raw_insight = ""
        warnings: list[AgentWarning] = []
        if interpret:
            try:
                insight, raw_insight = self.interpret(question, plan, result)
            except (LLMClientError, QueryValidationError) as exc:
                LOGGER.warning("agent_insight_unavailable error_type=%s", type(exc).__name__)
                warnings.append(
                    AgentWarning(
                        code="INSIGHT_UNAVAILABLE",
                        message="查询已完成，但 AI 文字解读暂时不可用，请稍后重试。",
                    )
                )

        return AgentRun(
            question=question,
            plan=plan,
            compiled_query=compiled_query,
            result=result,
            insight=insight,
            raw_plan_response=raw_plan,
            raw_insight_response=raw_insight,
            warnings=warnings,
            redact_compiled_query=self.trusted_facility_ids is not None,
        )

    def interpret(self, question: str, plan: AgentPlan, result: QueryResult) -> tuple[AgentInsight, str]:
        payload = {
            "question": question,
            "intent": plan.intent.model_dump(mode="json"),
            "analysis_goal": plan.analysis_goal,
            "assumptions": plan.assumptions,
            "query_spec": plan.query_spec.model_dump(mode="json") if plan.query_spec else None,
            "chart_spec": plan.chart_spec.model_dump(mode="json") if plan.chart_spec else None,
            "result": {
                "columns": result.columns,
                "rows": result.rows[:30],
                "row_count": result.row_count,
                "query_time_ms": result.query_time_ms,
                "truncated": result.truncated,
                "metadata": result.metadata,
            },
        }
        response = self.client.chat_json(
            [
                ChatMessage(role="system", content=_insight_system_prompt()),
                ChatMessage(role="user", content=json.dumps(payload, ensure_ascii=False)),
            ],
            max_tokens=1200,
        )
        try:
            data = extract_json_object(response)
            return AgentInsight.model_validate(data), response
        except (TypeError, ValueError) as exc:
            raise QueryValidationError(f"LLM produced invalid result insight: {exc}") from exc

    def _run_distinct_tool(self, tool_args: dict[str, Any]) -> dict[str, Any]:
        table = str(tool_args.get("table") or "inpatient")
        field = str(tool_args.get("field") or "")
        if not field:
            raise QueryValidationError("get_distinct_values requires tool_args.field")
        limit = int(tool_args.get("limit") or 30)
        if self.trusted_facility_ids is None:
            result = self.executor.fetch_distinct_values(table, field, limit)
        else:
            require_governed_distinct_field(table, field)
            effective_limit = min(
                max(limit, 1),
                self._max_distinct_values(),
                1000,
            )
            compiled_query = compile_scoped_query(
                {
                    "table": table,
                    "select": [field],
                    "filters": [{"field": field, "op": "is_not_null"}],
                    "group_by": [field],
                    "order_by": [{"field": field, "direction": "asc"}],
                    "limit": effective_limit,
                },
                trusted_facility_ids=self.trusted_facility_ids,
                privacy_min_group_size=self.privacy_min_group_size,
            )
            result = self._apply_minimum_group_privacy(
                self.executor.execute(compiled_query),
                safe_limit=compiled_query.limit,
            )
        return {
            "table": table,
            "field": field,
            "values": [row[field] for row in result.rows],
            "row_count": result.row_count,
            "truncated": result.truncated,
            "query_time_ms": result.query_time_ms,
            **({"metadata": result.metadata} if self.trusted_facility_ids is not None else {}),
        }

    def _compile_query(self, query_spec: QuerySpec | None) -> ExecutableQuery:
        """Compile a planned query with an out-of-band scope when configured."""

        if query_spec is None:
            raise QueryValidationError("query_medical_data requires query_spec")
        if self.trusted_facility_ids is None:
            return compile_query(query_spec)
        return compile_scoped_query(
            query_spec,
            trusted_facility_ids=self.trusted_facility_ids,
            privacy_min_group_size=self.privacy_min_group_size,
        )

    def _apply_minimum_group_privacy(
        self,
        result: QueryResult,
        *,
        safe_limit: int,
    ) -> QueryResult:
        """Sanitize scoped results before charting or LLM interpretation."""

        filtered = filter_minimum_group_rows(
            result.rows,
            result.columns,
            self.privacy_min_group_size,
        )
        return QueryResult(
            columns=filtered.columns,
            rows=filtered.rows,
            row_count=len(filtered.rows),
            query_time_ms=result.query_time_ms,
            truncated=len(filtered.rows) >= safe_limit,
            metadata=build_privacy_safe_metadata(
                result.metadata,
                self.privacy_min_group_size,
            ),
        )

    def _max_distinct_values(self) -> int:
        """Read the executor's configured distinct cap without exposing it."""

        settings = getattr(self.executor, "settings", None)
        configured_limit = getattr(settings, "query_max_distinct_values", 200)
        try:
            return max(int(configured_limit), 1)
        except (TypeError, ValueError):
            return 200


def _normalize_trusted_facility_scope(
    facility_ids: Iterable[str] | None,
) -> tuple[str, ...]:
    """Normalize a server-supplied facility scope and reject empty input."""

    if facility_ids is None or isinstance(facility_ids, (str, bytes, bytearray)):
        raise QueryValidationError("trusted facility scope must not be empty")

    normalized: set[str] = set()
    for facility_id in facility_ids:
        if not isinstance(facility_id, str) or not facility_id.strip():
            raise QueryValidationError(
                "trusted facility scope contains an invalid facility identifier"
            )
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


def normalize_agent_plan(data: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(data)
    if "querySpec" in normalized and "query_spec" not in normalized:
        normalized["query_spec"] = normalized.pop("querySpec")
    if "chart" in normalized and "chart_spec" not in normalized:
        normalized["chart_spec"] = normalized.pop("chart")

    intent = normalized.get("intent")
    if isinstance(intent, str):
        intent_type = intent
        normalized["intent"] = {
            "intent_type": intent_type,
            "route": _route_for_intent(intent_type),
            "confidence": normalized.pop("intent_confidence", 0.6),
            "reason": normalized.pop("intent_reason", "LLM classified the user request"),
        }
    elif isinstance(intent, dict):
        if "type" in intent and "intent_type" not in intent:
            intent["intent_type"] = intent.pop("type")
        if "route" not in intent:
            intent["route"] = _route_for_intent(str(intent.get("intent_type") or "aggregate"))
        if "confidence" not in intent:
            intent["confidence"] = normalized.pop("intent_confidence", 0.6)
        if "reason" not in intent:
            intent["reason"] = normalized.pop("intent_reason", "LLM classified the user request")
    elif "intent_type" in normalized:
        intent_type = str(normalized.pop("intent_type"))
        normalized["intent"] = {
            "intent_type": intent_type,
            "route": normalized.pop("route", _route_for_intent(intent_type)),
            "confidence": normalized.pop("intent_confidence", 0.6),
            "reason": normalized.pop("intent_reason", "LLM classified the user request"),
        }

    if "tool" in normalized and "tool_name" not in normalized:
        normalized["tool_name"] = normalized.pop("tool")
    if "tool_name" not in normalized:
        normalized["tool_name"] = _tool_for_route((normalized.get("intent") or {}).get("route", "query"))

    if isinstance(normalized.get("query_spec"), dict):
        normalized["query_spec"] = normalize_llm_queryspec(normalized["query_spec"])

    if isinstance(normalized.get("chart_spec"), dict):
        normalized["chart_spec"] = _normalize_chart_spec(normalized["chart_spec"])

    normalized.setdefault("analysis_goal", "Answer the user's data analysis question.")
    normalized.setdefault("assumptions", [])
    normalized.setdefault("execution_steps", [])
    normalized.setdefault("tool_args", {})
    return normalized


def _normalize_chart_spec(chart: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(chart)
    if "type" in normalized and "chart_type" not in normalized:
        normalized["chart_type"] = normalized.pop("type")
    if "x_field" not in normalized:
        if "x" in normalized:
            normalized["x_field"] = normalized["x"]
        elif "dimension" in normalized:
            normalized["x_field"] = normalized["dimension"]
    normalized.pop("x", None)
    normalized.pop("dimension", None)
    if "y_field" not in normalized:
        if "y" in normalized:
            normalized["y_field"] = normalized["y"]
        elif "metric" in normalized:
            normalized["y_field"] = normalized["metric"]
    normalized.pop("y", None)
    normalized.pop("metric", None)
    if "series" in normalized and "series_field" not in normalized:
        normalized["series_field"] = normalized.pop("series")
    normalized.setdefault("title", "Query result")
    normalized.setdefault("reason", "LLM selected this chart for the analysis result")
    return normalized


def _route_for_intent(intent_type: str) -> str:
    try:
        typed = IntentType(intent_type)
    except ValueError:
        return AgentRoute.QUERY.value
    if typed is IntentType.SCHEMA_LOOKUP:
        return AgentRoute.SCHEMA.value
    if typed is IntentType.DISTINCT_VALUES:
        return AgentRoute.DISTINCT.value
    if typed is IntentType.UNSUPPORTED:
        return AgentRoute.UNSUPPORTED.value
    return AgentRoute.QUERY.value


def _tool_for_route(route: str) -> str:
    if route == AgentRoute.SCHEMA.value:
        return ToolName.GET_DATABASE_SCHEMA.value
    if route == AgentRoute.DISTINCT.value:
        return ToolName.GET_DISTINCT_VALUES.value
    return ToolName.QUERY_MEDICAL_DATA.value


def _validated_or_recommended_chart(plan: AgentPlan, result: QueryResult) -> ChartSpec:
    if plan.chart_spec:
        result_columns = set(result.columns)
        referenced = {
            field
            for field in (plan.chart_spec.x_field, plan.chart_spec.y_field, plan.chart_spec.series_field)
            if field
        }
        if referenced.issubset(result_columns):
            return plan.chart_spec
    return recommend_chart(plan.query_spec, result.to_dict(), plan.intent)


def _planning_system_prompt() -> str:
    return (
        "You are a medical inpatient data analysis agent. "
        "Return JSON only. Do not return SQL or markdown. "
        "Your job is to understand the user's intent, choose exactly one tool, and prepare safe structured arguments. "
        "Available tools: get_database_schema, get_distinct_values, query_medical_data. "
        "For data analysis questions, choose query_medical_data and provide query_spec. "
        "For category/value questions like 'AgeGroup有哪些值', choose get_distinct_values and set tool_args with table, field, and limit. "
        "For schema questions, choose get_database_schema. "
        "For unsupported requests, set intent.intent_type to unsupported and explain in intent.reason. "
        "Never invent columns. Never produce arbitrary SQL. "
        "The only table is inpatient. "
        "Use TotalCharges for total charges, total bill, 总费用, 总收费, or 费用. "
        "Use TotalCosts only when the user explicitly asks for cost, costs, 总成本, or 成本. "
        "Use LengthOfStay for 住院天数, 住院时长, LOS, or length of stay. "
        "Use normalized values when filtering common fields: "
        "AgeGroup values are 0to17, 18to29, 30to49, 50to69, 70orOlder; "
        "Gender values are Female, Male, Unknown; "
        "EmergencyDepartmentIndicator values are Yes or No. "
        "Supported filter operators are =, !=, >, >=, <, <=, in, not_in, between, like, is_null, is_not_null. "
        "Supported aggregations are count, sum, avg, min, max. "
        "Metric objects must use field, agg, and alias; never use function or aggregation. "
        "Optional collection fields must be JSON arrays when present, never null. "
        "Supported chart_spec.chart_type values are table, bar, grouped_bar, line, pie, number. "
        "Chart objects must use chart_type, x_field, y_field, series_field, title, and reason; "
        "never use dimension or metric as chart keys. "
        "Use line charts for trends over DischargeYear, pie only for simple count/share distributions, grouped_bar for two grouped dimensions, and bar for ranked or comparison aggregates. "
        "Return this exact JSON shape: "
        "{"
        "\"intent\":{\"intent_type\":\"aggregate|comparison|trend|distribution|ranking|detail_lookup|schema_lookup|distinct_values|unsupported\","
        "\"route\":\"query|schema|distinct|unsupported\",\"confidence\":0.0,\"reason\":\"...\"},"
        "\"tool_name\":\"get_database_schema|get_distinct_values|query_medical_data\","
        "\"tool_args\":{},"
        "\"analysis_goal\":\"...\","
        "\"query_spec\":null,"
        "\"chart_spec\":null,"
        "\"assumptions\":[],"
        "\"execution_steps\":[]"
        "}. "
        "When query_spec is used, allowed QuerySpec keys are table, select, filters, group_by, metrics, order_by, limit. "
        "For aggregated questions, put dimensions in group_by and measures in metrics. "
        "Use metric aliases such as avg_total_charges, patient_count, total_costs, avg_length_of_stay. "
        f"Schema: {json.dumps(public_schema(), ensure_ascii=False)}"
    )


def _insight_system_prompt() -> str:
    return (
        "You explain structured query results for hospital inpatient discharge analytics. "
        "Return JSON only. Do not return markdown. "
        "Use only the provided result rows, QuerySpec, chart_spec, and metadata. "
        "Do not provide medical diagnosis, treatment advice, causal claims, or policy recommendations. "
        "Write in the same language as the user's question. "
        "Be concrete: mention the largest/smallest groups, obvious differences, row counts, truncation, and data limitations when visible. "
        "If the result is a small development subset, say that conclusions are demo-only if metadata or row counts suggest it. "
        "Return this exact JSON shape: "
        "{"
        "\"summary\":\"one short direct answer\","
        "\"chart_reading\":\"how to read the selected chart\","
        "\"observations\":[\"...\"],"
        "\"limitations\":[\"...\"],"
        "\"follow_up_questions\":[\"...\"]"
        "}."
    )
