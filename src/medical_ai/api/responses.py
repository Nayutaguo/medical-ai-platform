from __future__ import annotations

import time
from typing import Any

from flask import g, jsonify


def current_request_id() -> str:
    """Return the request correlation identifier created by the app hook."""

    return str(getattr(g, "request_id", "unknown"))


def success_response(
    data: dict[str, Any] | None,
    *,
    status: int = 200,
    dimensions: list[str] | None = None,
    metrics: list[str] | None = None,
    query_time_ms: float | None = None,
):
    """Build the stable success envelope used by every API endpoint."""

    return (
        jsonify(
            {
                "success": True,
                "data": data,
                "meta": _meta(dimensions, metrics, query_time_ms),
                "error": None,
            }
        ),
        status,
    )


def error_response(
    *,
    status: int,
    code: str,
    message: str,
    field_errors: list[dict[str, str]] | None = None,
):
    """Build the stable failure envelope without exposing stack traces."""

    error: dict[str, Any] = {"code": code, "message": message}
    if field_errors:
        error["field_errors"] = field_errors
    return (
        jsonify(
            {
                "success": False,
                "data": None,
                "meta": _meta([], [], None),
                "error": error,
            }
        ),
        status,
    )


def _meta(
    dimensions: list[str] | None,
    metrics: list[str] | None,
    query_time_ms: float | None,
) -> dict[str, Any]:
    started = float(getattr(g, "request_started", time.perf_counter()))
    total_time_ms = round((time.perf_counter() - started) * 1000, 3)
    return {
        "request_id": current_request_id(),
        "query_time_ms": round(query_time_ms, 3) if query_time_ms is not None else total_time_ms,
        "total_time_ms": total_time_ms,
        "dimensions": dimensions or [],
        "metrics": metrics or [],
    }
