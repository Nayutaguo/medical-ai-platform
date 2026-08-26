"""Versioned tenant administration routes behind session, RBAC, and CSRF."""

from __future__ import annotations

from typing import Any, cast

from flask import Blueprint, current_app, request

from medical_ai.api.errors import APIError
from medical_ai.api.responses import current_request_id, success_response
from medical_ai.api.security import access_context, governance_administration_service
from medical_ai.config import Settings
from medical_ai.identity.errors import AdministrationValidationError
from medical_ai.services.governance_administration import GovernanceAdministrationService


admin_api = Blueprint("admin_api", __name__, url_prefix="/api/v1/admin")


@admin_api.get("/members")
def list_members():
    _require_authentication_enabled()
    context = access_context()
    page = _service().list_members(
        context,
        cursor=_page_cursor(),
        limit=_page_limit(),
    )
    return success_response(page.to_public_dict())


@admin_api.get("/roles")
def list_roles():
    _require_authentication_enabled()
    context = access_context()
    page = _service().list_roles(
        context,
        cursor=_page_cursor(),
        limit=_page_limit(),
    )
    return success_response(page.to_public_dict())


@admin_api.get("/permissions")
def list_permissions():
    _require_authentication_enabled()
    context = access_context()
    page = _service().list_permissions(
        context,
        cursor=_page_cursor(),
        limit=_page_limit(),
    )
    return success_response(page.to_public_dict())


@admin_api.post("/roles")
def create_role():
    _require_authentication_enabled()
    context = access_context(require_csrf=True)
    payload = _json_object()
    _reject_unknown_fields(
        payload,
        {"role_key", "name", "description", "permission_ids"},
    )
    role = _service().create_role(
        context,
        role_key=_required_value(payload, "role_key"),
        name=_required_value(payload, "name"),
        description=payload.get("description"),
        permission_ids=_required_value(payload, "permission_ids"),
        request_id=current_request_id(),
    )
    return success_response(role.to_public_dict(), status=201)


@admin_api.put("/roles/<role_id>")
def update_role(role_id: str):
    _require_authentication_enabled()
    context = access_context(require_csrf=True)
    payload = _json_object()
    _reject_unknown_fields(
        payload,
        {"name", "description", "permission_ids", "expected_version"},
    )
    role = _service().update_role(
        context,
        role_id=role_id,
        name=_required_value(payload, "name"),
        description=payload.get("description"),
        permission_ids=_required_value(payload, "permission_ids"),
        expected_version=_required_value(payload, "expected_version"),
        request_id=current_request_id(),
    )
    return success_response(role.to_public_dict())


@admin_api.delete("/roles/<role_id>")
def delete_role(role_id: str):
    _require_authentication_enabled()
    context = access_context(require_csrf=True)
    payload = _json_object()
    _reject_unknown_fields(payload, {"expected_version"})
    _service().delete_role(
        context,
        role_id=role_id,
        expected_version=_required_value(payload, "expected_version"),
        request_id=current_request_id(),
    )
    return success_response({"deleted": True})


@admin_api.get("/facilities")
def list_facilities():
    _require_authentication_enabled()
    context = access_context()
    page = _service().list_facilities(
        context,
        cursor=_page_cursor(),
        limit=_page_limit(),
    )
    return success_response(page.to_public_dict())


@admin_api.get("/audit-events")
def list_audit_events():
    _require_authentication_enabled()
    context = access_context()
    page = _service().list_audit_events(
        context,
        cursor=request.args.get("cursor"),
        limit=_page_limit(),
        action=request.args.get("action"),
        outcome=request.args.get("outcome"),
    )
    return success_response(page.to_public_dict())


@admin_api.post("/invitations")
def create_invitation():
    _require_authentication_enabled()
    context = access_context(require_csrf=True)
    payload = _json_object()
    _reject_unknown_fields(payload, {"email", "lifetime_hours"})
    email = _required_string(payload, "email", maximum=320)
    lifetime_hours = _required_integer(payload, "lifetime_hours")
    invitation = _service().issue_invitation(
        context,
        email=email,
        lifetime_hours=lifetime_hours,
        request_id=current_request_id(),
    )
    return success_response(
        {
            "token": invitation.token,
            "expires_at": invitation.expires_at.isoformat(),
            "membership_id": invitation.membership_id,
        },
        status=201,
    )


@admin_api.put("/members/<membership_id>/roles")
def replace_member_roles(membership_id: str):
    _require_authentication_enabled()
    context = access_context(require_csrf=True)
    payload = _json_object()
    _reject_unknown_fields(payload, {"role_ids", "expected_version"})
    result = _service().replace_member_roles(
        context,
        membership_id=membership_id,
        role_ids=_required_value(payload, "role_ids"),
        expected_version=_required_value(payload, "expected_version"),
        request_id=current_request_id(),
    )
    return success_response(result.to_public_dict())


@admin_api.put("/members/<membership_id>/facility-scope")
def replace_member_facility_scope(membership_id: str):
    _require_authentication_enabled()
    context = access_context(require_csrf=True)
    payload = _json_object()
    _reject_unknown_fields(payload, {"facility_ids", "expected_version"})
    result = _service().replace_member_facility_scope(
        context,
        membership_id=membership_id,
        facility_ids=_required_value(payload, "facility_ids"),
        expected_version=_required_value(payload, "expected_version"),
        request_id=current_request_id(),
    )
    return success_response(result.to_public_dict())


@admin_api.patch("/members/<membership_id>/status")
def update_member_status(membership_id: str):
    _require_authentication_enabled()
    context = access_context(require_csrf=True)
    payload = _json_object()
    _reject_unknown_fields(payload, {"status", "expected_version"})
    result = _service().update_member_status(
        context,
        membership_id=membership_id,
        status=_required_value(payload, "status"),
        expected_version=_required_value(payload, "expected_version"),
        request_id=current_request_id(),
    )
    return success_response(result.to_public_dict())


@admin_api.post("/facilities/sync")
def sync_facilities():
    _require_authentication_enabled()
    context = access_context(require_csrf=True)
    payload = _json_object()
    _reject_unknown_fields(payload, set())
    result = _service().sync_facilities(
        context,
        request_id=current_request_id(),
    )
    return success_response(result.to_public_dict())


def _service() -> GovernanceAdministrationService:
    return governance_administration_service()


def _settings() -> Settings:
    return cast(Settings, current_app.extensions["settings"])


def _require_authentication_enabled() -> None:
    if not _settings().auth_enforcement_enabled:
        raise APIError(
            "AUTHENTICATION_DISABLED",
            "当前本地开发环境未启用身份认证",
            status=404,
        )


def _page_cursor() -> str | None:
    return request.args.get("cursor", default=None, type=str)


def _page_limit() -> int:
    raw = request.args.get("limit")
    if raw is None:
        return 50
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise AdministrationValidationError("limit 必须是整数") from exc


def _json_object() -> dict[str, Any]:
    if not request.is_json:
        raise APIError("UNSUPPORTED_MEDIA_TYPE", "请求必须使用 application/json", status=415)
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise AdministrationValidationError("请求体必须是 JSON 对象")
    return payload


def _reject_unknown_fields(payload: dict[str, Any], allowed: set[str]) -> None:
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise APIError(
            "ADMIN_INVALID_REQUEST",
            "请求包含不支持的字段",
            field_errors=[
                {"field": field, "message": "不支持的字段"}
                for field in unknown
            ],
        )


def _required_value(payload: dict[str, Any], field: str) -> object:
    if field not in payload:
        raise APIError(
            "ADMIN_INVALID_REQUEST",
            f"{field} 不能为空",
            field_errors=[{"field": field, "message": "不能为空"}],
        )
    return payload[field]


def _required_string(payload: dict[str, Any], field: str, *, maximum: int) -> str:
    value = _required_value(payload, field)
    if not isinstance(value, str) or not value.strip():
        raise APIError(
            "ADMIN_INVALID_REQUEST",
            f"{field} 不能为空",
            field_errors=[{"field": field, "message": "不能为空"}],
        )
    if len(value.encode("utf-8")) > maximum:
        raise APIError(
            "ADMIN_INVALID_REQUEST",
            f"{field} 超过长度限制",
            field_errors=[{"field": field, "message": f"最多 {maximum} 个 UTF-8 字节"}],
        )
    return value.strip()


def _required_integer(payload: dict[str, Any], field: str) -> int:
    value = _required_value(payload, field)
    if isinstance(value, bool) or not isinstance(value, int):
        raise APIError(
            "ADMIN_INVALID_REQUEST",
            f"{field} 必须是整数",
            field_errors=[{"field": field, "message": "必须是整数"}],
        )
    return value
