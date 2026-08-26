from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from medical_ai.api import create_app
from medical_ai.authorization import AccessContext, PermissionCode
from medical_ai.config import Settings
from medical_ai.identity.errors import (
    AdministrationLastManagerError,
    AdministrationResourceNotFoundError,
    AdministrationScopeConflictError,
    AdministrationSelfLockoutError,
    AdministrationStatusConflictError,
    AdministrationVersionConflictError,
    AuthenticationRequiredError,
    CsrfValidationError,
    FacilityCatalogInvalidError,
    FacilityOwnershipConflictError,
)
from medical_ai.identity.models import (
    ActiveSession,
    AdministrationFacility,
    AdministrationMember,
    AdministrationPage,
    AdministrationRole,
    FacilityCatalogSyncResult,
    IssuedUserInvitation,
)
from medical_ai.identity.tokens import hash_opaque_token
from medical_ai.services import ServiceResult


class MinimalAnalyticsService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def liveness(self) -> ServiceResult:
        return ServiceResult(data={"status": "alive"})


class FakeAuthenticationService:
    token = "session-token"
    csrf = "csrf-token"

    def __init__(self, context: AccessContext) -> None:
        now = datetime(2026, 8, 25, 9, 0)
        self.active = ActiveSession(
            session_id="session-1",
            user_id=context.user_id,
            email="admin@example.invalid",
            display_name="Admin",
            organization_name="Hospital One",
            expires_at=now + timedelta(hours=12),
            idle_expires_at=now + timedelta(minutes=30),
            csrf_token_hash=hash_opaque_token(self.csrf),
            access_context=context,
        )

    def resolve_session(self, token: str) -> ActiveSession:
        if token != self.token:
            raise AuthenticationRequiredError
        return self.active

    def require_csrf(self, _session: ActiveSession, token: str) -> None:
        if token != self.csrf:
            raise CsrfValidationError


class FakeAdministrationService:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.error: Exception | None = None
        role = AdministrationRole(
            id="role-1",
            role_key="analyst",
            name="Analyst",
            description="Read-only analyst",
            permissions=("analytics.query.execute",),
            version=1,
        )
        facility = AdministrationFacility(
            id="facility-1",
            facility_key="FACILITY-1",
            display_name="Facility One",
            status="active",
            version=1,
        )
        self.member_page = AdministrationPage(
            items=(
                AdministrationMember(
                    membership_id="member-1",
                    user_id="user-1",
                    email="member@example.invalid",
                    display_name="Member",
                    user_status="active",
                    membership_status="active",
                    authorization_version=1,
                    version=1,
                    roles=(role,),
                    facilities=(facility,),
                ),
            ),
            next_cursor="member-1",
        )
        self.role_page = AdministrationPage(items=(role,), next_cursor=None)
        self.facility_page = AdministrationPage(items=(facility,), next_cursor=None)
        self.mutation = replace(
            self.member_page.items[0],
            authorization_version=2,
            version=2,
        )

    def _raise(self) -> None:
        if self.error:
            raise self.error

    def list_members(self, context, *, cursor, limit):
        self._raise()
        self.calls.append(("members", context, cursor, limit))
        return self.member_page

    def list_roles(self, context, *, cursor, limit):
        self._raise()
        self.calls.append(("roles", context, cursor, limit))
        return self.role_page

    def list_facilities(self, context, *, cursor, limit):
        self._raise()
        self.calls.append(("facilities", context, cursor, limit))
        return self.facility_page

    def issue_invitation(self, context, **kwargs):
        self._raise()
        self.calls.append(("invitation", context, kwargs))
        return IssuedUserInvitation(
            token="secret-once",
            user_id="invited-user",
            organization_id=context.organization_id,
            membership_id="invited-membership",
            expires_at=datetime(2026, 8, 26, 9, 0, tzinfo=UTC),
        )

    def replace_member_roles(self, context, **kwargs):
        self._raise()
        self.calls.append(("replace_roles", context, kwargs))
        return self.mutation

    def replace_member_facility_scope(self, context, **kwargs):
        self._raise()
        self.calls.append(("replace_scope", context, kwargs))
        return self.mutation

    def update_member_status(self, context, **kwargs):
        self._raise()
        self.calls.append(("status", context, kwargs))
        return self.mutation

    def sync_facilities(self, context, **kwargs):
        self._raise()
        self.calls.append(("sync", context, kwargs))
        return FacilityCatalogSyncResult(created_count=2, existing_count=3)


def _context() -> AccessContext:
    return AccessContext(
        user_id="admin-user",
        organization_id="organization-1",
        membership_id="admin-membership",
        permissions=frozenset(PermissionCode),
        allowed_facility_ids=frozenset(),
        identity_version=1,
        authorization_version=1,
    )


def _client():
    settings = Settings(
        _env_file=None,
        auth_enforcement_enabled=True,
        auth_session_cookie_secure=False,
        auth_session_cookie_name="test_session",
    )
    authentication = FakeAuthenticationService(_context())
    administration = FakeAdministrationService()
    app = create_app(
        settings=settings,
        analytics_service=MinimalAnalyticsService(settings),
        authentication_service=authentication,
        governance_administration_service=administration,
    )
    app.config["TESTING"] = True
    return app.test_client(), authentication, administration


def _authenticate(client, authentication: FakeAuthenticationService) -> None:
    client.set_cookie("test_session", authentication.token, path="/api/v1")


def _write_headers(authentication: FakeAuthenticationService) -> dict[str, str]:
    return {"X-CSRF-Token": authentication.csrf}


def test_admin_catalog_requires_authentication_and_uses_bounded_page_contract() -> None:
    client, authentication, administration = _client()

    denied = client.get("/api/v1/admin/members")
    assert denied.status_code == 401
    assert administration.calls == []

    _authenticate(client, authentication)
    response = client.get("/api/v1/admin/members?cursor=member-0&limit=25")

    assert response.status_code == 200
    body = response.get_json()
    assert body["data"]["next_cursor"] == "member-1"
    assert body["data"]["items"][0] == {
        "membership_id": "member-1",
        "user_id": "user-1",
        "email": "member@example.invalid",
        "display_name": "Member",
        "user_status": "active",
        "membership_status": "active",
        "authorization_version": 1,
        "version": 1,
        "roles": [
            {
                "id": "role-1",
                "role_key": "analyst",
                "name": "Analyst",
                "description": "Read-only analyst",
                "permissions": ["analytics.query.execute"],
                "version": 1,
            }
        ],
        "facilities": [
            {
                "id": "facility-1",
                "facility_key": "FACILITY-1",
                "display_name": "Facility One",
                "status": "active",
                "version": 1,
            }
        ],
    }
    assert administration.calls[0][2:] == ("member-0", 25)


def test_role_and_facility_catalogs_share_the_list_envelope() -> None:
    client, authentication, _ = _client()
    _authenticate(client, authentication)

    roles = client.get("/api/v1/admin/roles").get_json()["data"]
    facilities = client.get("/api/v1/admin/facilities").get_json()["data"]

    assert roles == {
        "items": [
            {
                "id": "role-1",
                "role_key": "analyst",
                "name": "Analyst",
                "description": "Read-only analyst",
                "permissions": ["analytics.query.execute"],
                "version": 1,
            }
        ],
        "next_cursor": None,
    }
    assert facilities["items"][0]["facility_key"] == "FACILITY-1"
    assert facilities["next_cursor"] is None


def test_all_admin_writes_require_csrf_before_calling_service() -> None:
    client, authentication, administration = _client()
    _authenticate(client, authentication)

    responses = [
        client.post(
            "/api/v1/admin/invitations",
            json={"email": "new@example.invalid", "lifetime_hours": 24},
        ),
        client.put(
            "/api/v1/admin/members/member-1/roles",
            json={"role_ids": [], "expected_version": 1},
        ),
        client.put(
            "/api/v1/admin/members/member-1/facility-scope",
            json={"facility_ids": [], "expected_version": 1},
        ),
        client.patch(
            "/api/v1/admin/members/member-1/status",
            json={"status": "suspended", "expected_version": 1},
        ),
        client.post("/api/v1/admin/facilities/sync", json={}),
    ]

    assert {response.status_code for response in responses} == {403}
    assert {response.get_json()["error"]["code"] for response in responses} == {
        "CSRF_VALIDATION_FAILED"
    }
    assert administration.calls == []


def test_admin_write_routes_preserve_contract_and_request_id() -> None:
    client, authentication, administration = _client()
    _authenticate(client, authentication)
    headers = {**_write_headers(authentication), "X-Request-Id": "admin-request-1"}

    invitation = client.post(
        "/api/v1/admin/invitations",
        json={"email": "new@example.invalid", "lifetime_hours": 24},
        headers=headers,
    )
    roles = client.put(
        "/api/v1/admin/members/member-1/roles",
        json={"role_ids": ["role-1"], "expected_version": 1},
        headers=headers,
    )
    scope = client.put(
        "/api/v1/admin/members/member-1/facility-scope",
        json={"facility_ids": ["facility-1"], "expected_version": 1},
        headers=headers,
    )
    status = client.patch(
        "/api/v1/admin/members/member-1/status",
        json={"status": "suspended", "expected_version": 1},
        headers=headers,
    )
    sync = client.post(
        "/api/v1/admin/facilities/sync",
        json={},
        headers=headers,
    )

    assert invitation.status_code == 201
    assert invitation.get_json()["data"] == {
        "token": "secret-once",
        "expires_at": "2026-08-26T09:00:00+00:00",
        "membership_id": "invited-membership",
    }
    for response in (roles, scope, status):
        assert response.status_code == 200
        assert response.get_json()["data"] == {
            "membership_id": "member-1",
            "user_id": "user-1",
            "email": "member@example.invalid",
            "display_name": "Member",
            "user_status": "active",
            "membership_status": "active",
            "authorization_version": 2,
            "version": 2,
            "roles": [
                {
                    "id": "role-1",
                    "role_key": "analyst",
                    "name": "Analyst",
                    "description": "Read-only analyst",
                    "permissions": ["analytics.query.execute"],
                    "version": 1,
                }
            ],
            "facilities": [
                {
                    "id": "facility-1",
                    "facility_key": "FACILITY-1",
                    "display_name": "Facility One",
                    "status": "active",
                    "version": 1,
                }
            ],
        }
        assert response.headers["X-Request-Id"] == "admin-request-1"
    assert sync.get_json()["data"] == {"created_count": 2, "existing_count": 3}
    assert [call[0] for call in administration.calls] == [
        "invitation",
        "replace_roles",
        "replace_scope",
        "status",
        "sync",
    ]


def test_optimistic_conflict_maps_to_stable_409_without_leaking_state() -> None:
    client, authentication, administration = _client()
    _authenticate(client, authentication)
    administration.error = AdministrationVersionConflictError()

    response = client.put(
        "/api/v1/admin/members/member-1/roles",
        json={"role_ids": ["role-1"], "expected_version": 9},
        headers=_write_headers(authentication),
    )

    assert response.status_code == 409
    assert response.get_json()["error"]["code"] == "ADMIN_VERSION_CONFLICT"
    assert "role-1" not in response.get_data(as_text=True)


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (AdministrationResourceNotFoundError(), 404, "ADMIN_RESOURCE_NOT_FOUND"),
        (AdministrationScopeConflictError(), 409, "ADMIN_SCOPE_CONFLICT"),
        (AdministrationSelfLockoutError(), 409, "ADMIN_SELF_LOCKOUT"),
        (AdministrationLastManagerError(), 409, "ADMIN_LAST_MANAGER"),
        (AdministrationStatusConflictError(), 409, "ADMIN_STATUS_CONFLICT"),
        (FacilityOwnershipConflictError(), 409, "FACILITY_OWNERSHIP_CONFLICT"),
        (FacilityCatalogInvalidError(), 409, "FACILITY_CATALOG_INVALID"),
    ],
)
def test_administration_failures_have_stable_safe_error_contract(
    error: Exception,
    status: int,
    code: str,
) -> None:
    client, authentication, administration = _client()
    _authenticate(client, authentication)
    administration.error = error

    response = client.put(
        "/api/v1/admin/members/member-1/roles",
        json={"role_ids": ["role-1"], "expected_version": 1},
        headers=_write_headers(authentication),
    )

    assert response.status_code == status
    assert response.get_json()["error"]["code"] == code
    assert "organization-1" not in response.get_data(as_text=True)


def test_unknown_and_missing_fields_are_rejected_before_service() -> None:
    client, authentication, administration = _client()
    _authenticate(client, authentication)
    headers = _write_headers(authentication)

    unknown = client.patch(
        "/api/v1/admin/members/member-1/status",
        json={"status": "active", "expected_version": 1, "delete_user": True},
        headers=headers,
    )
    missing = client.put(
        "/api/v1/admin/members/member-1/roles",
        json={"expected_version": 1},
        headers=headers,
    )

    assert unknown.status_code == 400
    assert unknown.get_json()["error"]["code"] == "ADMIN_INVALID_REQUEST"
    assert missing.status_code == 400
    assert missing.get_json()["error"]["field_errors"] == [
        {"field": "role_ids", "message": "不能为空"}
    ]
    assert administration.calls == []
