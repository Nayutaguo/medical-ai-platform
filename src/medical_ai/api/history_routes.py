"""Authenticated HTTP adapter for per-membership analysis history."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar, cast

from flask import Blueprint, current_app, request

from medical_ai.api.errors import APIError
from medical_ai.api.responses import success_response
from medical_ai.api.security import access_context
from medical_ai.config import Settings
from medical_ai.history import (
    HistoryNotFoundError,
    HistoryService,
    HistoryValidationError,
    HistoryVersionConflictError,
    SqlAlchemyHistoryRepository,
)


history_api = Blueprint("history_api", __name__, url_prefix="/api/v1/history")
_T = TypeVar("_T")


@history_api.get("")
def list_history():
    """Return a bounded descending page for the current membership only."""

    _reject_unknown_query_parameters({"cursor", "limit", "favorite"})
    cursor = _optional_positive_query_integer("cursor")
    limit = _bounded_query_integer("limit", default=20, minimum=1, maximum=100)
    favorite = _optional_query_boolean("favorite")
    page = _invoke_history(
        get_history_service().list_history,
        access_context(),
        cursor=cursor,
        limit=limit,
        favorite=favorite,
    )
    return success_response(page.to_public_dict())


@history_api.patch("/<int:history_id>/favorite")
def update_favorite(history_id: int):
    """Set favorite state using the entry's optimistic-lock version."""

    payload = _json_object()
    unknown = set(payload) - {"is_favorite", "version", "expected_version"}
    if unknown:
        _invalid_request(f"不支持的字段: {', '.join(sorted(unknown))}")
    if "version" in payload and "expected_version" in payload:
        _invalid_request("version 与 expected_version 不能同时提供")

    is_favorite = payload.get("is_favorite")
    if not isinstance(is_favorite, bool):
        _invalid_request("is_favorite 必须是布尔值", field="is_favorite")
    raw_version = payload.get("version", payload.get("expected_version"))
    if isinstance(raw_version, bool) or not isinstance(raw_version, int) or raw_version < 1:
        _invalid_request("version 必须是正整数", field="version")

    entry = _invoke_history(
        get_history_service().set_favorite,
        access_context(require_csrf=True),
        history_id,
        is_favorite=is_favorite,
        expected_version=raw_version,
    )
    return success_response(entry.to_public_dict())


@history_api.delete("/<int:history_id>")
def delete_history(history_id: int):
    """Delete one entry from the current membership scope."""

    _invoke_history(
        get_history_service().delete_history,
        access_context(require_csrf=True),
        history_id,
    )
    return success_response({"deleted": True, "id": str(history_id)})


def get_history_service() -> HistoryService:
    """Return the injected history service, creating database infrastructure lazily."""

    service = current_app.extensions.get("history_service")
    if service is None:
        settings = cast(Settings, current_app.extensions["settings"])
        service = HistoryService(SqlAlchemyHistoryRepository(settings=settings))
        current_app.extensions["history_service"] = service
    return cast(HistoryService, service)


def _invoke_history(callback: Callable[..., _T], *args: Any, **kwargs: Any) -> _T:
    try:
        return callback(*args, **kwargs)
    except HistoryValidationError as exc:
        raise APIError("INVALID_HISTORY_REQUEST", str(exc)) from exc
    except HistoryNotFoundError as exc:
        raise APIError("HISTORY_NOT_FOUND", "查询历史不存在", status=404) from exc
    except HistoryVersionConflictError as exc:
        raise APIError(
            "HISTORY_VERSION_CONFLICT",
            "查询历史已被更新，请刷新后重试",
            status=409,
        ) from exc


def _json_object() -> dict[str, Any]:
    if not request.is_json:
        raise APIError("UNSUPPORTED_MEDIA_TYPE", "请求必须使用 application/json", status=415)
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise APIError("INVALID_REQUEST", "请求体必须是 JSON 对象")
    return payload


def _reject_unknown_query_parameters(allowed: set[str]) -> None:
    unknown = set(request.args) - allowed
    if unknown:
        _invalid_request(f"不支持的查询参数: {', '.join(sorted(unknown))}")


def _optional_positive_query_integer(name: str) -> int | None:
    raw = request.args.get(name)
    if raw is None:
        return None
    try:
        value = int(raw)
    except ValueError as exc:
        raise APIError(
            "INVALID_HISTORY_REQUEST",
            f"查询参数 {name} 必须是正整数",
        ) from exc
    if value < 1 or str(value) != raw:
        _invalid_request(f"查询参数 {name} 必须是正整数", field=name)
    return value


def _bounded_query_integer(name: str, *, default: int, minimum: int, maximum: int) -> int:
    raw = request.args.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise APIError(
            "INVALID_HISTORY_REQUEST",
            f"查询参数 {name} 必须是整数",
        ) from exc
    if value < minimum or value > maximum or str(value) != raw:
        _invalid_request(
            f"查询参数 {name} 必须在 {minimum} 到 {maximum} 之间",
            field=name,
        )
    return value


def _optional_query_boolean(name: str) -> bool | None:
    raw = request.args.get(name)
    if raw is None:
        return None
    if raw == "true":
        return True
    if raw == "false":
        return False
    _invalid_request(f"查询参数 {name} 必须是 true 或 false", field=name)
    raise AssertionError("unreachable")


def _invalid_request(message: str, *, field: str | None = None) -> None:
    field_errors = [{"field": field, "message": message}] if field else None
    raise APIError(
        "INVALID_HISTORY_REQUEST",
        message,
        field_errors=field_errors,
    )
