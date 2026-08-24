from __future__ import annotations

from typing import Any, cast

from flask import Blueprint, current_app, request

from medical_ai.api.errors import APIError
from medical_ai.api.responses import success_response
from medical_ai.api.security import access_context
from medical_ai.config import Settings
from medical_ai.services import AnalyticsService, ServiceResult

api_v1 = Blueprint("api_v1", __name__, url_prefix="/api/v1")


@api_v1.get("/health")
@api_v1.get("/health/live")
def health_live():
    """Dependency-free liveness probe; ``/health`` remains an alias."""

    return _respond(_service().liveness())


@api_v1.get("/health/ready")
def health_ready():
    """Readiness probe backed by a low-cost MySQL connectivity check."""

    return _respond(_service().readiness())


@api_v1.get("/schema")
def schema():
    if _auth_enforced():
        return _respond(_service().schema_authorized(access_context()))
    return _respond(_service().schema())


@api_v1.get("/distinct")
def distinct_values():
    table = _required_query_string("table", default="inpatient")
    field = _required_query_string("field")
    limit = _bounded_query_int(
        "limit",
        default=50,
        minimum=1,
        maximum=_service().settings.query_max_distinct_values,
    )
    if _auth_enforced():
        return _respond(
            _service().distinct_values_authorized(
                table,
                field,
                limit,
                access_context(),
            )
        )
    return _respond(_service().distinct_values(table, field, limit))


@api_v1.post("/query")
def query():
    payload = _json_object()
    query_spec = payload.get("query_spec", payload)
    if not isinstance(query_spec, dict):
        raise APIError(
            "INVALID_REQUEST",
            "query_spec 必须是 JSON 对象",
            field_errors=[{"field": "query_spec", "message": "必须是 JSON 对象"}],
        )
    if _auth_enforced():
        return _respond(
            _service().query_authorized(
                query_spec,
                access_context(require_csrf=True),
            )
        )
    return _respond(_service().query(query_spec))


@api_v1.post("/ask")
def ask():
    payload = _json_object()
    question = payload.get("question")
    if not isinstance(question, str) or not question.strip():
        raise APIError(
            "INVALID_REQUEST",
            "question 不能为空",
            field_errors=[{"field": "question", "message": "不能为空"}],
        )
    question = question.strip()
    if len(question) > _service().settings.api_max_question_chars:
        raise APIError(
            "INVALID_REQUEST",
            "question 超过长度限制",
            field_errors=[
                {
                    "field": "question",
                    "message": f"最多 {_service().settings.api_max_question_chars} 个字符",
                }
            ],
        )

    execute = payload.get("execute", True)
    if not isinstance(execute, bool):
        raise APIError(
            "INVALID_REQUEST",
            "execute 必须是布尔值",
            field_errors=[{"field": "execute", "message": "必须是布尔值"}],
        )
    if _auth_enforced():
        return _respond(
            _service().ask_authorized(
                question,
                access_context(require_csrf=True),
                execute=execute,
            )
        )
    return _respond(_service().ask(question, execute=execute))


def _service() -> AnalyticsService:
    return cast(AnalyticsService, current_app.extensions["analytics_service"])


def _settings() -> Settings:
    return cast(Settings, current_app.extensions["settings"])


def _auth_enforced() -> bool:
    """Use the governed path when explicitly enabled for this deployment."""

    return bool(getattr(_settings(), "auth_enforcement_enabled", False))


def _respond(result: ServiceResult):
    return success_response(
        result.data,
        dimensions=result.dimensions,
        metrics=result.metrics,
        query_time_ms=result.query_time_ms,
    )


def _json_object() -> dict[str, Any]:
    if not request.is_json:
        raise APIError("UNSUPPORTED_MEDIA_TYPE", "请求必须使用 application/json", status=415)
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise APIError("INVALID_REQUEST", "请求体必须是 JSON 对象")
    return payload


def _required_query_string(name: str, default: str | None = None) -> str:
    value = request.args.get(name, default=default, type=str)
    if value is None or not value.strip():
        raise APIError(
            "INVALID_REQUEST",
            f"查询参数 {name} 不能为空",
            field_errors=[{"field": name, "message": "不能为空"}],
        )
    return value.strip()


def _bounded_query_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = request.args.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise APIError(
            "INVALID_REQUEST",
            f"查询参数 {name} 必须是整数",
            field_errors=[{"field": name, "message": "必须是整数"}],
        ) from exc
    if value < minimum or value > maximum:
        raise APIError(
            "INVALID_REQUEST",
            f"查询参数 {name} 必须在 {minimum} 到 {maximum} 之间",
            field_errors=[{"field": name, "message": f"范围为 {minimum}..{maximum}"}],
        )
    return value
