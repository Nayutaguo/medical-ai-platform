from __future__ import annotations

from datetime import UTC, datetime

import pytest

from medical_ai.authorization import AccessContext, PermissionCode
from medical_ai.authorization.errors import PermissionDeniedError
from medical_ai.identity.errors import (
    AdministrationSelfLockoutError,
    AdministrationValidationError,
)
from medical_ai.identity.models import (
    AdministrationMember,
    AdministrationPage,
    FacilityCatalogSyncResult,
    IssuedUserInvitation,
)
from medical_ai.services.governance_administration import GovernanceAdministrationService


class FakeRepository:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.page = AdministrationPage(items=(), next_cursor=None)
        self.mutation = AdministrationMember(
            membership_id="target-membership",
            user_id="target-user",
            email="target@example.invalid",
            display_name="Target Member",
            user_status="active",
            membership_status="active",
            authorization_version=2,
            version=2,
        )

    def list_administration_members(self, organization_id, *, cursor, limit):
        self.calls.append(("members", organization_id, cursor, limit))
        return self.page

    def list_administration_roles(self, organization_id, *, cursor, limit):
        self.calls.append(("roles", organization_id, cursor, limit))
        return self.page

    def list_administration_facilities(self, organization_id, *, cursor, limit):
        self.calls.append(("facilities", organization_id, cursor, limit))
        return self.page

    def replace_membership_roles(self, **kwargs):
        self.calls.append(("replace_roles", kwargs))
        return self.mutation

    def replace_membership_facility_scope(self, **kwargs):
        self.calls.append(("replace_scope", kwargs))
        return self.mutation

    def update_membership_status(self, **kwargs):
        self.calls.append(("status", kwargs))
        return self.mutation

    def sync_organization_facilities(self, **kwargs):
        self.calls.append(("sync", kwargs))
        return FacilityCatalogSyncResult(created_count=3, existing_count=2)


class FakeInvitationService:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def issue_invitation(self, **kwargs):
        self.calls.append(kwargs)
        return IssuedUserInvitation(
            token="one-time-token",
            user_id="invited-user",
            organization_id=kwargs["organization_id"],
            membership_id="invited-membership",
            expires_at=datetime(2026, 8, 26, tzinfo=UTC),
        )


def _context(*permissions: PermissionCode) -> AccessContext:
    return AccessContext(
        user_id="actor-user",
        organization_id="actor-organization",
        membership_id="actor-membership",
        permissions=frozenset(permissions),
        allowed_facility_ids=frozenset(),
        identity_version=1,
        authorization_version=1,
    )


def _service() -> tuple[GovernanceAdministrationService, FakeRepository, FakeInvitationService]:
    repository = FakeRepository()
    invitations = FakeInvitationService()
    return (
        GovernanceAdministrationService(
            repository,  # type: ignore[arg-type]
            invitation_service=invitations,  # type: ignore[arg-type]
        ),
        repository,
        invitations,
    )


@pytest.mark.parametrize(
    "permission",
    [PermissionCode.USERS_MANAGE, PermissionCode.ROLES_ASSIGN],
)
def test_member_catalog_accepts_either_exact_administration_permission(permission) -> None:
    service, repository, _ = _service()

    page = service.list_members(_context(permission), cursor="cursor-1", limit=25)

    assert page.items == ()
    assert repository.calls == [("members", "actor-organization", "cursor-1", 25)]


def test_role_catalog_requires_roles_assign_and_facilities_allow_imports() -> None:
    service, repository, _ = _service()

    with pytest.raises(PermissionDeniedError):
        service.list_roles(_context(PermissionCode.USERS_MANAGE))
    with pytest.raises(PermissionDeniedError):
        service.list_facilities(_context(PermissionCode.USERS_MANAGE))
    with pytest.raises(PermissionDeniedError):
        service.list_roles(_context(PermissionCode.IMPORTS_CREATE))

    service.list_roles(_context(PermissionCode.ROLES_ASSIGN))
    service.list_facilities(_context(PermissionCode.ROLES_ASSIGN))
    service.list_facilities(_context(PermissionCode.IMPORTS_CREATE))
    assert [call[0] for call in repository.calls] == [
        "roles",
        "facilities",
        "facilities",
    ]


def test_import_permission_does_not_expose_members() -> None:
    service, repository, _ = _service()

    with pytest.raises(PermissionDeniedError):
        service.list_members(_context(PermissionCode.IMPORTS_CREATE))

    assert repository.calls == []


def test_invitation_is_bound_to_authenticated_actor_and_users_permission() -> None:
    service, _, invitations = _service()
    now = datetime(2026, 8, 25, 8, 0, tzinfo=UTC)

    issued = service.issue_invitation(
        _context(PermissionCode.USERS_MANAGE),
        email=" Analyst@Example.com ",
        lifetime_hours=24,
        request_id="request-1",
        now=now,
    )

    assert issued.token == "one-time-token"
    call = invitations.calls[0]
    assert call["organization_id"] == "actor-organization"
    assert call["email"] == "analyst@example.com"
    assert call["actor"].membership_id == "actor-membership"
    assert call["lifetime"].total_seconds() == 24 * 3600

    with pytest.raises(PermissionDeniedError):
        service.issue_invitation(
            _context(PermissionCode.ROLES_ASSIGN),
            email="analyst@example.com",
            lifetime_hours=24,
            request_id=None,
        )


@pytest.mark.parametrize("email", ["ü@example.com", "josé@example.com"])
def test_administration_invitation_rejects_non_ascii_email(email: str) -> None:
    service, _, invitations = _service()

    with pytest.raises(AdministrationValidationError, match="email"):
        service.issue_invitation(
            _context(PermissionCode.USERS_MANAGE),
            email=email,
            lifetime_hours=24,
            request_id=None,
        )

    assert invitations.calls == []


@pytest.mark.parametrize("hours", [0, 169, True, "24"])
def test_invitation_lifetime_is_strictly_bounded(hours: object) -> None:
    service, _, _ = _service()
    with pytest.raises(AdministrationValidationError):
        service.issue_invitation(
            _context(PermissionCode.USERS_MANAGE),
            email="analyst@example.com",
            lifetime_hours=hours,  # type: ignore[arg-type]
            request_id=None,
        )


def test_role_and_scope_writes_validate_bounded_unique_identifiers() -> None:
    service, repository, _ = _service()
    context = _context(PermissionCode.ROLES_ASSIGN)

    service.replace_member_roles(
        context,
        membership_id="target-membership",
        role_ids=["role-1"],
        expected_version=1,
        request_id="request-roles",
    )
    service.replace_member_facility_scope(
        context,
        membership_id="target-membership",
        facility_ids=[],
        expected_version=1,
        request_id="request-scope",
    )

    assert [call[0] for call in repository.calls] == ["replace_roles", "replace_scope"]
    with pytest.raises(AdministrationValidationError):
        service.replace_member_roles(
            context,
            membership_id="target-membership",
            role_ids=["role-1", "role-1"],
            expected_version=1,
            request_id=None,
        )
    with pytest.raises(AdministrationValidationError):
        service.replace_member_facility_scope(
            context,
            membership_id="target-membership",
            facility_ids="facility-1",
            expected_version=1,
            request_id=None,
        )


def test_status_write_requires_users_manage_and_blocks_self_lockout() -> None:
    service, repository, _ = _service()

    with pytest.raises(PermissionDeniedError):
        service.update_member_status(
            _context(PermissionCode.ROLES_ASSIGN),
            membership_id="target-membership",
            status="suspended",
            expected_version=1,
            request_id=None,
        )

    with pytest.raises(AdministrationSelfLockoutError):
        service.update_member_status(
            _context(PermissionCode.USERS_MANAGE),
            membership_id="actor-membership",
            status="suspended",
            expected_version=1,
            request_id=None,
        )
    with pytest.raises(AdministrationValidationError):
        service.update_member_status(
            _context(PermissionCode.USERS_MANAGE),
            membership_id="target-membership",
            status="removed",
            expected_version=1,
            request_id=None,
        )
    assert repository.calls == []


@pytest.mark.parametrize(
    "permission",
    [PermissionCode.IMPORTS_CREATE, PermissionCode.ROLES_ASSIGN],
)
def test_facility_sync_accepts_import_or_role_administration(permission) -> None:
    service, repository, _ = _service()

    result = service.sync_facilities(_context(permission), request_id="request-sync")

    assert result == FacilityCatalogSyncResult(created_count=3, existing_count=2)
    assert repository.calls[0][0] == "sync"


def test_page_and_version_limits_fail_before_repository_access() -> None:
    service, repository, _ = _service()
    with pytest.raises(AdministrationValidationError):
        service.list_members(_context(PermissionCode.USERS_MANAGE), limit=101)
    with pytest.raises(AdministrationValidationError):
        service.replace_member_roles(
            _context(PermissionCode.ROLES_ASSIGN),
            membership_id="target-membership",
            role_ids=[],
            expected_version=0,
            request_id=None,
        )
    assert repository.calls == []
