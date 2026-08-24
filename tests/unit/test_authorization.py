from dataclasses import FrozenInstanceError

import pytest

from medical_ai.authorization import (
    AccessContext,
    AuthorizationService,
    EmptyFacilityScopeError,
    OrganizationScopeDeniedError,
    PermissionCode,
    PermissionDeniedError,
    authorize_facility_scope,
    require_permission,
    resolve_facility_scope,
)


def _context(
    *,
    permissions: frozenset[PermissionCode] | None = None,
    facilities: frozenset[str] | None = None,
) -> AccessContext:
    return AccessContext(
        user_id="user-1",
        organization_id="org-1",
        membership_id="membership-1",
        permissions=permissions or frozenset({PermissionCode.ANALYTICS_QUERY_EXECUTE}),
        allowed_facility_ids=facilities or frozenset({"facility-a", "facility-b"}),
        identity_version=2,
        authorization_version=5,
    )


def test_permission_codes_are_stable() -> None:
    assert {permission.value for permission in PermissionCode} == {
        "analytics.query.execute",
        "analytics.agent.execute",
        "analytics.schema.read",
        "analytics.distinct.read",
        "users.manage",
        "roles.assign",
        "audit.read",
        "imports.create",
    }


def test_access_context_is_immutable_and_contains_no_credentials() -> None:
    context = AccessContext(
        user_id=" user-1 ",
        organization_id="org-1",
        membership_id="membership-1",
        permissions=frozenset({"analytics.query.execute"}),
        allowed_facility_ids=frozenset({" facility-a "}),
        identity_version=1,
        authorization_version=1,
    )

    assert context.user_id == "user-1"
    assert context.permissions == frozenset({PermissionCode.ANALYTICS_QUERY_EXECUTE})
    assert context.allowed_facility_ids == frozenset({"facility-a"})
    assert not hasattr(context, "password")
    assert not hasattr(context, "token")
    with pytest.raises(FrozenInstanceError):
        setattr(context, "user_id", "other-user")


@pytest.mark.parametrize("field", ["identity_version", "authorization_version"])
def test_access_context_rejects_non_positive_versions(field: str) -> None:
    values = {
        "user_id": "user-1",
        "organization_id": "org-1",
        "membership_id": "membership-1",
        "permissions": frozenset({PermissionCode.ANALYTICS_QUERY_EXECUTE}),
        "allowed_facility_ids": frozenset({"facility-a"}),
        "identity_version": 1,
        "authorization_version": 1,
    }
    values[field] = 0

    with pytest.raises(ValueError, match="greater than or equal to 1"):
        AccessContext(**values)


def test_require_permission_has_stable_denial() -> None:
    context = _context()

    require_permission(context, PermissionCode.ANALYTICS_QUERY_EXECUTE)
    with pytest.raises(PermissionDeniedError) as error:
        require_permission(context, PermissionCode.ANALYTICS_AGENT_EXECUTE)

    assert error.value.error_code == "permission_denied"
    assert error.value.permission == "analytics.agent.execute"


def test_requested_facility_scope_can_only_shrink_allowed_scope() -> None:
    context = _context()

    effective_scope = resolve_facility_scope(
        context,
        ["facility-b", "facility-not-authorized"],
        organization_id="org-1",
    )

    assert effective_scope == frozenset({"facility-b"})
    assert "facility-not-authorized" not in effective_scope


def test_omitted_facility_scope_uses_only_membership_allowlist() -> None:
    context = _context(facilities=frozenset({"facility-a"}))

    assert resolve_facility_scope(context) == frozenset({"facility-a"})


def test_cross_organization_scope_is_rejected() -> None:
    with pytest.raises(OrganizationScopeDeniedError) as error:
        resolve_facility_scope(
            _context(),
            ["facility-a"],
            organization_id="org-2",
        )

    assert error.value.error_code == "organization_scope_denied"


@pytest.mark.parametrize("requested", [[], ["facility-c"], [" "]])
def test_empty_or_disjoint_facility_scope_is_rejected(requested: list[str]) -> None:
    with pytest.raises(EmptyFacilityScopeError) as error:
        resolve_facility_scope(_context(), requested)

    assert error.value.error_code == "empty_facility_scope"


def test_administrative_and_analytics_permissions_do_not_imply_each_other() -> None:
    administrator = _context(permissions=frozenset({PermissionCode.USERS_MANAGE}))
    analyst = _context(permissions=frozenset({PermissionCode.ANALYTICS_QUERY_EXECUTE}))

    require_permission(administrator, PermissionCode.USERS_MANAGE)
    require_permission(analyst, PermissionCode.ANALYTICS_QUERY_EXECUTE)
    with pytest.raises(PermissionDeniedError):
        require_permission(administrator, PermissionCode.ANALYTICS_QUERY_EXECUTE)
    with pytest.raises(PermissionDeniedError):
        require_permission(analyst, PermissionCode.USERS_MANAGE)


def test_atomic_authorization_checks_permission_before_scope() -> None:
    context = _context(permissions=frozenset({PermissionCode.USERS_MANAGE}))

    with pytest.raises(PermissionDeniedError):
        authorize_facility_scope(
            context,
            PermissionCode.ANALYTICS_QUERY_EXECUTE,
            ["facility-a"],
        )


def test_authorization_service_exposes_the_same_pure_policy() -> None:
    service = AuthorizationService()
    context = _context()

    assert service.authorize_facility_scope(
        context,
        PermissionCode.ANALYTICS_QUERY_EXECUTE,
        ["facility-a", "facility-c"],
        organization_id="org-1",
    ) == frozenset({"facility-a"})
