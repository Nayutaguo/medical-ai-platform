from __future__ import annotations

import json
import re
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from medical_ai.agent.llm_client import ChatMessage, OpenAICompatibleClient
from medical_ai.db.schema import column_names, public_schema
from medical_ai.query import QuerySpec, QueryValidationError, validate_query_spec


@dataclass(frozen=True)
class PlannedQuery:
    question: str
    query_spec: QuerySpec
    raw_response: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "query_spec": self.query_spec.model_dump(mode="json"),
            "raw_response": self.raw_response,
        }


class QueryPlanner:
    def __init__(self, client: OpenAICompatibleClient | None = None) -> None:
        self.client = client or OpenAICompatibleClient()

    def plan(self, question: str) -> PlannedQuery:
        response = self.client.chat_json(
            [
                ChatMessage(role="system", content=_system_prompt()),
                ChatMessage(role="user", content=question),
            ]
        )
        data = normalize_llm_queryspec(extract_json_object(response))
        try:
            spec = validate_query_spec(data)
        except QueryValidationError as exc:
            raise QueryValidationError(f"LLM produced invalid QuerySpec: {exc}. Raw response: {response}") from exc
        return PlannedQuery(question=question, query_spec=spec, raw_response=response)


def extract_json_object(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text, flags=re.IGNORECASE).strip()
        text = re.sub(r"```$", "", text).strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise
        value = json.loads(text[start : end + 1])
    if not isinstance(value, dict):
        raise ValueError("LLM response JSON must be an object")
    return value


def normalize_llm_queryspec(data: dict[str, Any]) -> dict[str, Any]:
    normalized = deepcopy(data)

    # LLMs sometimes emit JSON null for optional arrays. Treat it as omission so
    # QuerySpec's safe empty-list defaults apply; validation still enforces every
    # supplied item and the aggregate-only API rejects an empty metrics list.
    for field in ("select", "filters", "group_by", "metrics", "order_by"):
        if normalized.get(field) is None:
            normalized.pop(field, None)

    filters = normalized.get("filters")
    if isinstance(filters, list):
        for item in filters:
            if isinstance(item, dict) and "op" not in item and "operator" in item:
                item["op"] = item["operator"]
                item.pop("operator", None)
            if isinstance(item, dict) and isinstance(item.get("op"), str):
                item["op"] = item["op"].lower()

    group_by = normalized.get("group_by")
    if isinstance(group_by, list):
        normalized["group_by"] = [
            item["field"] if isinstance(item, dict) and set(item) == {"field"} else item for item in group_by
        ]

    metrics = normalized.get("metrics")
    if isinstance(metrics, list):
        for item in metrics:
            if isinstance(item, dict) and "agg" not in item:
                if "function" in item:
                    item["agg"] = item["function"]
                elif "aggregation" in item:
                    item["agg"] = item["aggregation"]
            if isinstance(item, dict):
                item.pop("function", None)
                item.pop("aggregation", None)
            if isinstance(item, dict) and isinstance(item.get("agg"), str):
                item["agg"] = item["agg"].lower()
            if isinstance(item, dict) and item.get("agg") == "count" and "field" not in item:
                item["field"] = "*"

    order_by = normalized.get("order_by")
    if isinstance(order_by, list):
        for item in order_by:
            if isinstance(item, dict) and "direction" not in item and "dir" in item:
                item["direction"] = item["dir"]
                item.pop("dir", None)
            if isinstance(item, dict) and isinstance(item.get("direction"), str):
                item["direction"] = item["direction"].lower()

    table = normalized.get("table")
    if normalized.get("limit") is None:
        normalized.pop("limit", None)

    select_fields = normalized.get("select")
    if isinstance(table, str) and isinstance(select_fields, list) and isinstance(metrics, list):
        try:
            allowed_fields = set(column_names(table))
        except KeyError:
            allowed_fields = set()
        metric_aliases = {metric.get("alias") for metric in metrics if isinstance(metric, dict)}
        metric_fields = {metric.get("field") for metric in metrics if isinstance(metric, dict)}
        group_fields = normalized.get("group_by")
        group_field_set = set(group_fields) if isinstance(group_fields, list) and all(isinstance(f, str) for f in group_fields) else set()
        normalized["select"] = [
            field
            for field in select_fields
            if not _is_redundant_metric_select(field, allowed_fields, metric_aliases, metric_fields, group_field_set)
        ]

    return normalized


def _is_redundant_metric_select(
    field: Any,
    allowed_fields: set[str],
    metric_aliases: set[Any],
    metric_fields: set[Any],
    group_fields: set[str],
) -> bool:
    if not isinstance(field, str):
        return False
    if field in allowed_fields:
        return bool(group_fields and field in metric_fields and field not in group_fields)
    if field in metric_aliases:
        return True
    return bool(re.match(r"^\s*(count|sum|avg|min|max)\s*\(", field, flags=re.IGNORECASE)) or bool(
        re.search(r"\s+as\s+", field, flags=re.IGNORECASE)
    )


def _system_prompt() -> str:
    schema = public_schema()
    return (
        "You convert user questions about hospital inpatient discharge data into a strict QuerySpec JSON object. "
        "Return JSON only. Do not return SQL. Do not include markdown. "
        "The only table is inpatient. Use only fields from the schema. "
        "Use the filter key named op, never operator. "
        "Use metric objects with agg, field, and alias; never use function. "
        "Use group_by as a list of field name strings; never objects. "
        "Use order_by direction, never dir. "
        "When metrics and group_by are present, select should contain only group_by fields or be omitted. "
        "Do not put SQL expressions such as avg(TotalCharges) in select; use metrics instead. "
        "Use TotalCharges for total charges, total bill, 总费用, or 总收费. "
        "Use TotalCosts only when the user explicitly asks for cost, costs, 总成本, or 成本. "
        "Use normalized values when filtering common fields: "
        "AgeGroup values are 0to17, 18to29, 30to49, 50to69, 70orOlder; "
        "Gender values are Female, Male, Unknown; "
        "EmergencyDepartmentIndicator values are Yes or No. "
        "Supported filter operators are =, !=, >, >=, <, <=, in, not_in, between, like, is_null, is_not_null. "
        "Supported aggregations are count, sum, avg, min, max. "
        "Allowed QuerySpec keys are table, select, filters, group_by, metrics, order_by, limit. "
        "For aggregated questions, put dimensions in group_by and measures in metrics. "
        "Use clear aliases such as avg_total_charges or patient_count. "
        f"Schema: {json.dumps(schema, ensure_ascii=False)}"
    )
