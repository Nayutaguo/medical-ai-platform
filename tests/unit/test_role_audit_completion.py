from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from medical_ai.api import create_app
from medical_ai.authorization import AccessContext, PermissionCode
from medical_ai.authorization.errors import PermissionDeniedError
from medical_ai.config import Settings
from medical_ai.identity.errors import AdministrationValidationError, CsrfValidationError
from medical_ai.identity.models import (
    ActiveSession,
    AdministrationAuditEvent,
    AdministrationAuditPage,
    AdministrationPage,
    AdministrationPermission,
    AdministrationRole,
)
from medical_ai.identity.tokens import hash_opaque_token
from medical_ai.services import ServiceResult
from medical_ai.services.governance_administration import GovernanceAdministrationService


NOW = datetime(2026, 8, 26, 9, 0)


class GovernanceRepository:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.role = AdministrationRole(
            id="role-custom",
            role_key="quality_analyst",
            name="Quality analyst",
            description="Aggregate quality analysis",
            permissions=(PermissionCode.ANALYTICS_QUERY_EXECUTE.value,),
            version=1,
            is_system=False,
        )

    def list_administration_permissions(self, *, cursor, limit):
        self.calls.append(("permissions", cursor, limit))
        return AdministrationPage(items=(), next_cursor=None)

    def create_administration_role(self, **kwargs):
        self.calls.append(("create", kwargs))
        return self.role

    def update_administration_role(self, **kwargs):
        self.calls.append(("update", kwargs))
        return self.role

    def delete_administration_role(self, **kwargs):
        self.calls.append(("delete", kwargs))

    def list_administration_audit_events(self, context, **kwargs):
        self.calls.append(("audit", context.organization_id, kwargs))
        return AdministrationAuditPage(items=(), next_cursor=None)


class InvitationService:
    pass


def _context(*permissions: PermissionCode) -> AccessContext:
    return AccessContext(
        user_id="actor-user",
        organization_id="organization-1",
        membership_id="actor-membership",
        permissions=frozenset(permissions),
        allowed_facility_ids=frozenset(),
        identity_version=1,
        authorization_version=1,
        session_id="session-1",
    )


def test_role_service_validates_permission_ids_and_uses_roles_assign() -> None:
    repository = GovernanceRepository()
    service = GovernanceAdministrationService(
        repository,  # type: ignore[arg-type]
        invitation_service=InvitationService(),  # type: ignore[arg-type]
    )
    denied = _context(PermissionCode.USERS_MANAGE)
    with pytest.raises(PermissionDeniedError):
        service.list_permissions(denied)

    context = _context(PermissionCode.ROLES_ASSIGN)
    result = service.create_role(
        context,
        role_key="Quality_Analyst",
        name=" Quality analyst ",
        description=" Aggregate quality analysis ",
        permission_ids=["permission-query"],
        request_id="request-create",
        now=NOW,
    )
    assert result.id == "role-custom"
    call = repository.calls[-1][1]
    assert call["role_key"] == "quality_analyst"
    assert call["permission_ids"] == ("permission-query",)
    assert call["request_id"] == "request-create"

    with pytest.raises(AdministrationValidationError):
        service.create_role(
            context,
            role_key="bad role key",
            name="Role",
            description=None,
            permission_ids=[],
            request_id=None,
        )
    with pytest.raises(AdministrationValidationError):
        service.update_role(
            context,
            role_id="role-custom",
            name="Role",
            description=None,
            permission_ids=["permission-query", "permission-query"],
            expected_version=1,
            request_id=None,
        )


def test_audit_service_requires_audit_read_and_validates_keyset_filters() -> None:
    repository = GovernanceRepository()
    service = GovernanceAdministrationService(
        repository,  # type: ignore[arg-type]
        invitation_service=InvitationService(),  # type: ignore[arg-type]
    )
    with pytest.raises(PermissionDeniedError):
        service.list_audit_events(_context(PermissionCode.ROLES_ASSIGN))

    service.list_audit_events(
        _context(PermissionCode.AUDIT_READ),
        cursor="42",
        limit=20,
        action="identity.role.update",
        outcome="SUCCESS",
        now=NOW,
    )
    assert repository.calls[-1] == (
        "audit",
        "organization-1",
        {
            "cursor": "42",
            "limit": 20,
            "action": "identity.role.update",
            "outcome": "success",
            "read_at": NOW,
        },
    )
    with pytest.raises(AdministrationValidationError):
        service.list_audit_events(
            _context(PermissionCode.AUDIT_READ),
            cursor="not-a-number",
        )


class MinimalAnalyticsService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def liveness(self) -> ServiceResult:
        return ServiceResult(data={"status": "alive"})


class RouteAuthentication:
    token = "route-session"
    csrf = "route-csrf"

    def __init__(self) -> None:
        context = _context(
            PermissionCode.ROLES_ASSIGN,
            PermissionCode.AUDIT_READ,
        )
        self.active = ActiveSession(
            session_id="session-1",
            user_id="actor-user",
            email="admin@example.com",
            display_name="Admin",
            organization_name="Organization 1",
            expires_at=NOW + timedelta(hours=1),
            idle_expires_at=NOW + timedelta(minutes=30),
            csrf_token_hash=hash_opaque_token(self.csrf),
            access_context=context,
        )

    def resolve_session(self, token: str):
        return self.active if token == self.token else None

    def require_csrf(self, _session, token: str):
        if token != self.csrf:
            raise CsrfValidationError


class RouteGovernance:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.permission = AdministrationPermission(
            id="permission-query",
            permission_key="analytics.query.execute",
            resource="analytics.query",
            action="execute",
            description="Run aggregate queries",
            version=1,
        )
        self.role = AdministrationRole(
            id="role-custom",
            role_key="quality_analyst",
            name="Quality analyst",
            description=None,
            permissions=("analytics.query.execute",),
            version=2,
            is_system=False,
        )

    def list_permissions(self, context, *, cursor, limit):
        self.calls.append(("permissions", context, cursor, limit))
        return AdministrationPage(items=(self.permission,), next_cursor=None)

    def create_role(self, context, **kwargs):
        self.calls.append(("create", context, kwargs))
        return self.role

    def update_role(self, context, **kwargs):
        self.calls.append(("update", context, kwargs))
        return self.role

    def delete_role(self, context, **kwargs):
        self.calls.append(("delete", context, kwargs))

    def list_audit_events(self, context, **kwargs):
        self.calls.append(("audit", context, kwargs))
        event = AdministrationAuditEvent(
            id=9,
            occurred_at=NOW,
            request_id="request-9",
            actor_kind="user",
            actor_user_id="actor-user",
            action="identity.role.update",
            resource_type="role",
            resource_id="role-custom",
            outcome="success",
            error_code=None,
            details={"role_keys": ["quality_analyst"]},
        )
        return AdministrationAuditPage(items=(event,), next_cursor=None)


def _route_client():
    settings = Settings(
        _env_file=None,
        auth_enforcement_enabled=True,
        auth_session_cookie_secure=False,
        auth_session_cookie_name="test_session",
    )
    authentication = RouteAuthentication()
    governance = RouteGovernance()
    app = create_app(
        settings=settings,
        analytics_service=MinimalAnalyticsService(settings),
        authentication_service=authentication,
        governance_administration_service=governance,
    )
    app.config["TESTING"] = True
    client = app.test_client()
    client.set_cookie("test_session", authentication.token, path="/api/v1")
    return client, authentication, governance


def test_admin_permission_role_and_audit_routes_match_frontend_contract() -> None:
    client, authentication, governance = _route_client()

    permission_response = client.get("/api/v1/admin/permissions")
    assert permission_response.status_code == 200
    assert permission_response.get_json()["data"]["items"][0] == {
        "id": "permission-query",
        "permission_key": "analytics.query.execute",
        "resource": "analytics.query",
        "action": "execute",
        "description": "Run aggregate queries",
        "version": 1,
    }

    created = client.post(
        "/api/v1/admin/roles",
        json={
            "role_key": "quality_analyst",
            "name": "Quality analyst",
            "description": "",
            "permission_ids": ["permission-query"],
        },
        headers={"X-CSRF-Token": authentication.csrf},
    )
    assert created.status_code == 201
    assert created.get_json()["data"]["is_system"] is False
    assert governance.calls[-1][2]["permission_ids"] == ["permission-query"]

    deleted = client.delete(
        "/api/v1/admin/roles/role-custom",
        json={"expected_version": 2},
        headers={"X-CSRF-Token": authentication.csrf},
    )
    assert deleted.get_json()["data"] == {"deleted": True}

    audit = client.get(
        "/api/v1/admin/audit-events?action=identity.role.update&outcome=success"
    )
    event = audit.get_json()["data"]["items"][0]
    assert event == {
        "id": 9,
        "occurred_at": "2026-08-26T09:00:00",
        "request_id": "request-9",
        "actor_kind": "user",
        "actor_user_id": "actor-user",
        "action": "identity.role.update",
        "resource_type": "role",
        "resource_id": "role-custom",
        "outcome": "success",
        "error_code": None,
        "details": {"role_keys": ["quality_analyst"]},
    }
    assert "actor_membership_id" not in event
