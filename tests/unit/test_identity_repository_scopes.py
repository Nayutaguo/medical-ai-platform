from __future__ import annotations

from datetime import datetime, timedelta
from hashlib import sha256

import pytest
from sqlalchemy import Integer, create_engine, event, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import StaticPool

from medical_ai.authorization import (
    AccessContext,
    PermissionCode,
    PermissionDeniedError,
)
from medical_ai.config import Settings
from medical_ai.db.schema import build_sqlalchemy_table
from medical_ai.identity import (
    audit_events,
    auth_sessions,
    facilities,
    identity_metadata,
    membership_facility_scopes,
    membership_roles,
    one_time_tokens,
    organization_facilities,
    organization_memberships,
    organizations,
    permissions,
    role_permissions,
    roles,
    users,
)
from medical_ai.identity.models import UserInvitationPlan
from medical_ai.identity.errors import (
    AdministrationLastManagerError,
    AdministrationResourceNotFoundError,
    AdministrationScopeConflictError,
    AdministrationSelfLockoutError,
    AdministrationStatusConflictError,
    AdministrationVersionConflictError,
    AuthenticationRequiredError,
    FacilityCatalogScopeUnavailableError,
    FacilityOwnershipConflictError,
)
from medical_ai.repositories import IdentityRepository


NOW = datetime(2026, 2, 1, 12, 0, 0)


@pytest.fixture
def identity_repository() -> tuple[IdentityRepository, object]:
    engine = create_engine(
        "sqlite+pysqlite://",
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _configure_sqlite(dbapi_connection, _connection_record) -> None:
        dbapi_connection.execute("PRAGMA foreign_keys=ON")
        dbapi_connection.create_function(
            "UTC_TIMESTAMP",
            1,
            lambda _precision: "2026-01-01 00:00:00.000000",
        )

    # SQLite autoincrements only an exact INTEGER PRIMARY KEY. Production MySQL
    # retains the schema's BIGINT audit identifier.
    audit_id_type = audit_events.c.id.type
    audit_events.c.id.type = Integer()
    try:
        identity_metadata.create_all(engine)
    finally:
        audit_events.c.id.type = audit_id_type
    build_sqlalchemy_table("inpatient").create(engine)
    _seed_identity_data(engine)
    repository = IdentityRepository(
        settings=Settings(
            _env_file=None,
            inpatient_dataset_owner_organization_id="organization-1",
        ),
        engine=engine,
    )
    return repository, engine


def test_membership_grants_aggregate_permissions_and_active_facilities_without_cartesian_noise(
    identity_repository,
) -> None:
    repository, _engine = identity_repository

    grants = {
        grant.membership_id: grant
        for grant in repository.list_active_memberships("user-1")
    }

    assert set(grants) == {"membership-1", "membership-2", "membership-empty"}
    assert grants["membership-1"].permissions == frozenset(
        {
            PermissionCode.ANALYTICS_QUERY_EXECUTE,
            PermissionCode.ANALYTICS_SCHEMA_READ,
            PermissionCode.ROLES_ASSIGN,
            PermissionCode.USERS_MANAGE,
        }
    )
    assert grants["membership-1"].allowed_facility_ids == frozenset(
        {"FACILITY-A", "FACILITY-B"}
    )
    assert grants["membership-2"].allowed_facility_ids == frozenset(
        {"FACILITY-OTHER-ORG"}
    )
    assert grants["membership-empty"].allowed_facility_ids == frozenset()


def test_cross_organization_scope_is_rejected_by_composite_membership_context(
    identity_repository,
) -> None:
    _repository, engine = identity_repository

    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                membership_facility_scopes.insert().values(
                    membership_id="membership-1",
                    organization_id="organization-2",
                    facility_id="facility-other-org",
                )
            )


def test_active_session_uses_the_same_active_fail_closed_facility_scope(
    identity_repository,
) -> None:
    repository, _engine = identity_repository

    session = repository.find_active_session(b"s" * 32, now=NOW)

    assert session is not None
    assert session.access_context.membership_id == "membership-1"
    assert session.access_context.organization_id == "organization-1"
    assert session.access_context.permissions == frozenset(
        {
            PermissionCode.ANALYTICS_QUERY_EXECUTE,
            PermissionCode.ANALYTICS_SCHEMA_READ,
            PermissionCode.ROLES_ASSIGN,
            PermissionCode.USERS_MANAGE,
        }
    )
    assert session.access_context.allowed_facility_ids == frozenset(
        {"FACILITY-A", "FACILITY-B"}
    )
    assert "FACILITY-DISABLED" not in session.access_context.allowed_facility_ids


def test_soft_deleted_identity_or_organization_is_immediately_unavailable(
    identity_repository,
) -> None:
    repository, engine = identity_repository
    with engine.begin() as connection:
        connection.execute(
            users.update().where(users.c.id == "user-1").values(deleted_at=NOW)
        )

    assert repository.find_active_session(b"s" * 32, now=NOW) is None
    assert repository.list_active_memberships("user-1") == []

    with engine.begin() as connection:
        connection.execute(
            users.update().where(users.c.id == "user-1").values(deleted_at=None)
        )
        connection.execute(
            organizations.update()
            .where(organizations.c.id == "organization-1")
            .values(deleted_at=NOW)
        )

    assert repository.find_active_session(b"s" * 32, now=NOW) is None
    assert {
        grant.organization_id
        for grant in repository.list_active_memberships("user-1")
    } == {"organization-2", "organization-empty"}


def test_session_touch_does_not_move_idle_deadline_backwards(
    identity_repository,
) -> None:
    repository, engine = identity_repository

    repository.touch_session(
        "session-1",
        seen_at=NOW + timedelta(minutes=10),
        idle_expires_at=NOW + timedelta(minutes=40),
    )
    repository.touch_session(
        "session-1",
        seen_at=NOW + timedelta(minutes=5),
        idle_expires_at=NOW + timedelta(minutes=35),
    )

    with engine.connect() as connection:
        row = connection.execute(
            select(
                auth_sessions.c.last_seen_at,
                auth_sessions.c.idle_expires_at,
                auth_sessions.c.version,
            ).where(auth_sessions.c.id == "session-1")
        ).mappings().one()

    assert row["last_seen_at"] == NOW + timedelta(minutes=10)
    assert row["idle_expires_at"] == NOW + timedelta(minutes=40)
    assert row["version"] == 2


def _admin_context(*permissions: PermissionCode) -> AccessContext:
    return AccessContext(
        user_id="user-1",
        organization_id="organization-1",
        membership_id="membership-1",
        permissions=frozenset(permissions or tuple(PermissionCode)),
        allowed_facility_ids=frozenset({"FACILITY-A", "FACILITY-B"}),
        identity_version=1,
        authorization_version=1,
        session_id="session-1",
    )


def test_administration_member_page_is_tenant_bound_and_keyset_paginated(
    identity_repository,
) -> None:
    repository, _engine = identity_repository

    first = repository.list_administration_members(
        "organization-1",
        cursor=None,
        limit=1,
    )
    second = repository.list_administration_members(
        "organization-1",
        cursor=first.next_cursor,
        limit=1,
    )

    assert [item.membership_id for item in first.items] == ["membership-1"]
    assert first.next_cursor == "membership-1"
    assert [item.membership_id for item in second.items] == ["membership-target"]
    assert second.next_cursor is None
    member = first.items[0]
    assert member.email == "analyst@example.invalid"
    assert {role.id for role in member.roles} == {
        "role-1",
        "role-2",
        "role-admin",
    }
    role_by_id = {role.id: role for role in member.roles}
    assert role_by_id["role-1"].description is None
    assert role_by_id["role-1"].permissions == (
        "analytics.future.capability",
        "analytics.query.execute",
    )
    assert {facility.id for facility in member.facilities} == {
        "facility-a",
        "facility-b",
        "facility-disabled",
    }
    facility_by_id = {facility.id: facility for facility in member.facilities}
    assert facility_by_id["facility-disabled"].status == "disabled"
    assert "membership-2" not in {
        item.membership_id for item in first.items + second.items
    }


def test_invited_member_projection_does_not_reveal_global_identity_source(
    identity_repository,
) -> None:
    repository, engine = identity_repository
    with engine.begin() as connection:
        connection.execute(
            users.insert(),
            [
                {
                    "id": "source-user-active",
                    "email": "Existing.Active@Example.Invalid",
                    "email_normalized": "existing.active@example.invalid",
                    "display_name": "Existing Global Profile",
                    "password_hash": "opaque-existing-verifier",
                    "password_algorithm": "argon2id",
                    "status": "active",
                },
                {
                    "id": "source-user-invited",
                    "email": "Existing.Invited@Example.Invalid",
                    "email_normalized": "existing.invited@example.invalid",
                    "display_name": "Different Pending Profile",
                    "password_hash": None,
                    "password_algorithm": None,
                    "status": "invited",
                },
            ],
        )

    sources = (
        ("new", "New.Source@Example.Invalid", "new.source@example.invalid"),
        (
            "active",
            "existing.active@example.invalid",
            "existing.active@example.invalid",
        ),
        (
            "invited",
            "existing.invited@example.invalid",
            "existing.invited@example.invalid",
        ),
    )
    membership_ids: dict[str, str] = {}
    for suffix, email, email_normalized in sources:
        persisted = repository.create_user_invitation(
            UserInvitationPlan(
                token_id=f"source-token-{suffix}",
                user_id=f"source-candidate-{suffix}",
                email=email,
                email_normalized=email_normalized,
                placeholder_display_name="Invited user",
                organization_id="organization-1",
                membership_id=f"invited-source-{suffix}",
                identity_version=1,
                token_hash=sha256(suffix.encode("ascii")).digest(),
                expires_at=NOW + timedelta(hours=1),
                created_at=NOW,
                request_id=f"source-{suffix}",
                actor_kind="system",
                actor_membership_id=None,
                actor_user_id=None,
            )
        )
        membership_ids[suffix] = persisted.membership_id

    page = repository.list_administration_members(
        "organization-1",
        cursor=None,
        limit=100,
    )
    by_membership = {item.membership_id: item for item in page.items}

    for suffix, _email, email_normalized in sources:
        item = by_membership[membership_ids[suffix]]
        assert item.email == email_normalized
        assert item.display_name == "Invited user"
        assert item.user_status == "invited"
        assert item.membership_status == "invited"
        assert item.roles == ()
        assert item.facilities == ()

    assert "Existing Global Profile" not in {
        item.display_name for item in by_membership.values()
        if item.membership_id in membership_ids.values()
    }


def test_role_and_facility_catalogs_include_governance_metadata(
    identity_repository,
) -> None:
    repository, _engine = identity_repository

    role_page = repository.list_administration_roles(
        "organization-1",
        cursor=None,
        limit=10,
    )
    facility_page = repository.list_administration_facilities(
        "organization-1",
        cursor=None,
        limit=10,
    )

    role_by_id = {role.id: role for role in role_page.items}
    assert role_by_id["role-admin"].permissions == (
        PermissionCode.ROLES_ASSIGN.value,
        PermissionCode.USERS_MANAGE.value,
    )
    assert role_by_id["role-admin"].description is None
    facility_by_id = {facility.id: facility for facility in facility_page.items}
    assert facility_by_id["facility-a"].status == "active"
    assert facility_by_id["facility-disabled"].status == "disabled"
    assert "facility-other-org" not in facility_by_id


def test_role_replacement_audits_and_rebases_only_the_requesting_session(
    identity_repository,
) -> None:
    repository, engine = identity_repository

    result = repository.replace_membership_roles(
        context=_admin_context(),
        membership_id="membership-1",
        role_ids=("role-admin",),
        expected_version=1,
        request_id="request-role-update",
        occurred_at=NOW,
    )

    assert result.version == 2
    assert result.authorization_version == 2
    assert result.roles[0].id == "role-admin"
    assert result.roles[0].permissions == (
        PermissionCode.ROLES_ASSIGN.value,
        PermissionCode.USERS_MANAGE.value,
    )
    active_session = repository.find_active_session(b"s" * 32, now=NOW)
    assert active_session is not None
    assert active_session.access_context.authorization_version == 2
    assert active_session.access_context.session_id == "session-1"
    assert repository.find_active_session(b"o" * 32, now=NOW) is None
    with engine.connect() as connection:
        assigned = connection.execute(
            select(membership_roles.c.role_id).where(
                membership_roles.c.membership_id == "membership-1"
            )
        ).scalars().all()
        audit = connection.execute(
            select(audit_events.c.action, audit_events.c.details).where(
                audit_events.c.request_id == "request-role-update"
            )
        ).mappings().one()
    assert assigned == ["role-admin"]
    assert audit["action"] == "identity.membership.roles.update"
    assert audit["details"] == {
        "previous_role_keys": [
            "analyst-primary",
            "analyst-secondary",
            "organization-admin",
        ],
        "new_role_keys": ["organization-admin"],
    }


def test_role_replacement_blocks_cross_tenant_and_self_lockout_atomically(
    identity_repository,
) -> None:
    repository, engine = identity_repository
    context = _admin_context()

    with pytest.raises(AdministrationScopeConflictError):
        repository.replace_membership_roles(
            context=context,
            membership_id="membership-1",
            role_ids=("role-other-org",),
            expected_version=1,
            request_id="cross-tenant-role",
            occurred_at=NOW,
        )
    with pytest.raises(AdministrationResourceNotFoundError):
        repository.replace_membership_roles(
            context=context,
            membership_id="membership-2",
            role_ids=("role-admin",),
            expected_version=1,
            request_id="cross-tenant-member",
            occurred_at=NOW,
        )
    with pytest.raises(AdministrationSelfLockoutError):
        repository.replace_membership_roles(
            context=context,
            membership_id="membership-1",
            role_ids=("role-1",),
            expected_version=1,
            request_id="self-lockout-role",
            occurred_at=NOW,
        )

    with engine.connect() as connection:
        assigned = set(
            connection.execute(
                select(membership_roles.c.role_id).where(
                    membership_roles.c.membership_id == "membership-1"
                )
            ).scalars()
        )
        version = connection.execute(
            select(organization_memberships.c.version).where(
                organization_memberships.c.id == "membership-1"
            )
        ).scalar_one()
        audit_count = connection.execute(
            select(func.count()).select_from(audit_events).where(
                audit_events.c.request_id.in_(
                    (
                        "cross-tenant-role",
                        "cross-tenant-member",
                        "self-lockout-role",
                    )
                )
            )
        ).scalar_one()
    assert assigned == {"role-1", "role-2", "role-admin"}
    assert version == 1
    assert audit_count == 0


def test_consecutive_self_role_and_scope_updates_rebase_only_current_session(
    identity_repository,
) -> None:
    repository, _engine = identity_repository

    role_result = repository.replace_membership_roles(
        context=_admin_context(),
        membership_id="membership-1",
        role_ids=("role-users-manager", "role-roles-manager"),
        expected_version=1,
        request_id="self-role-update",
        occurred_at=NOW,
    )
    assert role_result.authorization_version == 2
    rebased = repository.find_active_session(b"s" * 32, now=NOW)
    assert rebased is not None
    assert rebased.access_context.authorization_version == 2
    assert rebased.access_context.permissions == frozenset(
        {PermissionCode.USERS_MANAGE, PermissionCode.ROLES_ASSIGN}
    )
    assert repository.find_active_session(b"o" * 32, now=NOW) is None

    scope_result = repository.replace_membership_facility_scope(
        context=rebased.access_context,
        membership_id="membership-1",
        facility_ids=("facility-a",),
        expected_version=2,
        request_id="self-scope-update",
        occurred_at=NOW + timedelta(seconds=1),
    )

    assert scope_result.authorization_version == 3
    active = repository.find_active_session(
        b"s" * 32,
        now=NOW + timedelta(seconds=1),
    )
    assert active is not None
    assert active.access_context.authorization_version == 3
    assert active.access_context.allowed_facility_ids == frozenset({"FACILITY-A"})


def test_administration_write_rejects_stale_or_revoked_actor_authority(
    identity_repository,
) -> None:
    repository, engine = identity_repository
    with engine.begin() as connection:
        connection.execute(
            organization_memberships.update()
            .where(organization_memberships.c.id == "membership-1")
            .values(authorization_version=2)
        )

    with pytest.raises(AuthenticationRequiredError):
        repository.replace_membership_facility_scope(
            context=_admin_context(),
            membership_id="membership-target",
            facility_ids=("facility-a",),
            expected_version=1,
            request_id="stale-actor",
            occurred_at=NOW,
        )

    with engine.begin() as connection:
        connection.execute(
            organization_memberships.update()
            .where(organization_memberships.c.id == "membership-1")
            .values(authorization_version=1)
        )
        connection.execute(
            auth_sessions.update()
            .where(auth_sessions.c.id == "session-1")
            .values(revoked_at=NOW)
        )

    with pytest.raises(AuthenticationRequiredError):
        repository.replace_membership_facility_scope(
            context=_admin_context(),
            membership_id="membership-target",
            facility_ids=("facility-a",),
            expected_version=1,
            request_id="revoked-actor",
            occurred_at=NOW + timedelta(seconds=1),
        )

    with engine.connect() as connection:
        assert connection.execute(
            select(func.count()).select_from(audit_events).where(
                audit_events.c.request_id.in_(("stale-actor", "revoked-actor"))
            )
        ).scalar_one() == 0


def test_administration_write_rechecks_current_effective_permission(
    identity_repository,
) -> None:
    repository, engine = identity_repository
    with engine.begin() as connection:
        connection.execute(
            membership_roles.delete().where(
                membership_roles.c.membership_id == "membership-1"
            )
        )
        connection.execute(
            membership_roles.insert().values(
                organization_id="organization-1",
                membership_id="membership-1",
                role_id="role-users-manager",
            )
        )

    with pytest.raises(PermissionDeniedError):
        repository.replace_membership_facility_scope(
            context=_admin_context(),
            membership_id="membership-target",
            facility_ids=("facility-a",),
            expected_version=1,
            request_id="permission-removed",
            occurred_at=NOW,
        )

    with engine.connect() as connection:
        assert connection.execute(
            select(func.count()).select_from(audit_events).where(
                audit_events.c.request_id == "permission-removed"
            )
        ).scalar_one() == 0


def test_user_actor_invitation_revalidates_exact_session_inside_transaction(
    identity_repository,
) -> None:
    repository, engine = identity_repository

    def invitation_plan(*, suffix: str, request_id: str) -> UserInvitationPlan:
        return UserInvitationPlan(
            token_id=f"token-{suffix}",
            user_id=f"invited-user-{suffix}",
            email=f"invite-{suffix}@example.invalid",
            email_normalized=f"invite-{suffix}@example.invalid",
            placeholder_display_name="Invited user",
            organization_id="organization-1",
            membership_id=f"invited-membership-{suffix}",
            identity_version=1,
            token_hash=suffix.encode("utf-8").ljust(32, b"x"),
            expires_at=NOW + timedelta(hours=1),
            created_at=NOW,
            request_id=request_id,
            actor_kind="user",
            actor_membership_id="membership-1",
            actor_user_id="user-1",
            actor_identity_version=1,
            actor_authorization_version=1,
            actor_session_id="session-1",
        )

    created = repository.create_user_invitation(
        invitation_plan(suffix="valid", request_id="actor-invite-valid")
    )
    assert created.membership_id == "invited-membership-valid"

    with engine.begin() as connection:
        connection.execute(
            auth_sessions.update()
            .where(auth_sessions.c.id == "session-1")
            .values(revoked_at=NOW)
        )

    with pytest.raises(AuthenticationRequiredError):
        repository.create_user_invitation(
            invitation_plan(suffix="revoked", request_id="actor-invite-revoked")
        )

    with engine.connect() as connection:
        assert connection.execute(
            select(func.count()).select_from(one_time_tokens).where(
                one_time_tokens.c.id == "token-valid"
            )
        ).scalar_one() == 1
        assert connection.execute(
            select(func.count()).select_from(users).where(
                users.c.id == "invited-user-revoked"
            )
        ).scalar_one() == 0
        assert connection.execute(
            select(func.count()).select_from(audit_events).where(
                audit_events.c.request_id == "actor-invite-revoked"
            )
        ).scalar_one() == 0


def test_facility_scope_replacement_is_tenant_bound_versioned_and_audited(
    identity_repository,
) -> None:
    repository, engine = identity_repository

    result = repository.replace_membership_facility_scope(
        context=_admin_context(),
        membership_id="membership-target",
        facility_ids=("facility-a",),
        expected_version=1,
        request_id="request-scope-update",
        occurred_at=NOW,
    )

    assert result.membership_id == "membership-target"
    assert result.authorization_version == 2
    assert result.version == 2
    assert [facility.id for facility in result.facilities] == ["facility-a"]
    with engine.connect() as connection:
        assigned = connection.execute(
            select(membership_facility_scopes.c.facility_id).where(
                membership_facility_scopes.c.membership_id == "membership-target"
            )
        ).scalars().all()
        audit = connection.execute(
            select(audit_events.c.details).where(
                audit_events.c.request_id == "request-scope-update"
            )
        ).scalar_one()
    assert assigned == ["facility-a"]
    assert audit == {
        "previous_facility_count": 0,
        "new_facility_count": 1,
        "previous_facility_scope_digest": f"sha256:{sha256(b'').hexdigest()}",
        "new_facility_scope_digest": (
            f"sha256:{sha256(b'facility-a').hexdigest()}"
        ),
    }

    with pytest.raises(AdministrationScopeConflictError):
        repository.replace_membership_facility_scope(
            context=_admin_context(),
            membership_id="membership-target",
            facility_ids=("facility-other-org",),
            expected_version=2,
            request_id="cross-tenant-scope",
            occurred_at=NOW,
        )


def test_membership_status_update_never_deletes_user_and_blocks_self_suspend(
    identity_repository,
) -> None:
    repository, engine = identity_repository

    result = repository.update_membership_status(
        context=_admin_context(),
        membership_id="membership-target",
        status="suspended",
        expected_version=1,
        request_id="request-status-update",
        occurred_at=NOW,
    )

    assert result.authorization_version == 2
    assert result.membership_status == "suspended"
    with engine.connect() as connection:
        membership = connection.execute(
            select(
                organization_memberships.c.status,
                organization_memberships.c.version,
            ).where(organization_memberships.c.id == "membership-target")
        ).one()
        user_exists = connection.execute(
            select(users.c.id).where(users.c.id == "user-target")
        ).scalar_one()
        audit = connection.execute(
            select(audit_events.c.details).where(
                audit_events.c.request_id == "request-status-update"
            )
        ).scalar_one()
    assert membership == ("suspended", 2)
    assert user_exists == "user-target"
    assert audit == {"previous_status": "active", "new_status": "suspended"}

    with pytest.raises(AdministrationSelfLockoutError):
        repository.update_membership_status(
            context=_admin_context(),
            membership_id="membership-1",
            status="suspended",
            expected_version=1,
            request_id="self-suspend",
            occurred_at=NOW,
        )


def test_last_active_management_member_is_guarded_by_role_and_status_writes(
    identity_repository,
) -> None:
    repository, engine = identity_repository
    context = _admin_context()
    with engine.begin() as connection:
        connection.execute(
            membership_roles.delete().where(
                membership_roles.c.membership_id == "membership-1"
            )
        )
        connection.execute(
            membership_roles.insert().values(
                organization_id="organization-1",
                membership_id="membership-1",
                role_id="role-roles-manager",
            )
        )
        connection.execute(
            membership_roles.insert().values(
                organization_id="organization-1",
                membership_id="membership-target",
                role_id="role-admin",
            )
        )
        connection.execute(
            users.insert(),
            [
                {
                    "id": "user-disabled-backup",
                    "email": "disabled-backup@example.invalid",
                    "email_normalized": "disabled-backup@example.invalid",
                    "display_name": "Disabled Backup",
                    "status": "disabled",
                    "deleted_at": None,
                },
                {
                    "id": "user-deleted-backup",
                    "email": "deleted-backup@example.invalid",
                    "email_normalized": "deleted-backup@example.invalid",
                    "display_name": "Deleted Backup",
                    "status": "active",
                    "deleted_at": NOW,
                },
            ],
        )
        connection.execute(
            organization_memberships.insert(),
            [
                {
                    "id": "membership-disabled-backup",
                    "organization_id": "organization-1",
                    "user_id": "user-disabled-backup",
                    "status": "active",
                },
                {
                    "id": "membership-deleted-backup",
                    "organization_id": "organization-1",
                    "user_id": "user-deleted-backup",
                    "status": "active",
                },
            ],
        )
        connection.execute(
            membership_roles.insert(),
            [
                {
                    "organization_id": "organization-1",
                    "membership_id": "membership-disabled-backup",
                    "role_id": "role-admin",
                },
                {
                    "organization_id": "organization-1",
                    "membership_id": "membership-deleted-backup",
                    "role_id": "role-admin",
                },
            ],
        )

    with pytest.raises(AdministrationLastManagerError):
        repository.replace_membership_roles(
            context=context,
            membership_id="membership-target",
            role_ids=(),
            expected_version=1,
            request_id="remove-last-manager",
            occurred_at=NOW,
        )
    with pytest.raises(AdministrationLastManagerError):
        with engine.begin() as connection:
            connection.execute(
                membership_roles.delete().where(
                    membership_roles.c.membership_id == "membership-1"
                )
            )
            connection.execute(
                membership_roles.insert().values(
                    organization_id="organization-1",
                    membership_id="membership-1",
                    role_id="role-users-manager",
                )
            )
        repository.update_membership_status(
            context=context,
            membership_id="membership-target",
            status="suspended",
            expected_version=1,
            request_id="suspend-last-manager",
            occurred_at=NOW,
        )

    with engine.begin() as connection:
        connection.execute(
            membership_roles.delete().where(
                membership_roles.c.membership_id == "membership-1"
            )
        )
        connection.execute(
            membership_roles.insert(),
            [
                {
                    "organization_id": "organization-1",
                    "membership_id": "membership-1",
                    "role_id": "role-users-manager",
                },
                {
                    "organization_id": "organization-1",
                    "membership_id": "membership-1",
                    "role_id": "role-roles-manager",
                },
            ],
        )
    result = repository.replace_membership_roles(
        context=context,
        membership_id="membership-target",
        role_ids=(),
        expected_version=1,
        request_id="remove-redundant-manager",
        occurred_at=NOW,
    )

    assert result.roles == ()
    with engine.connect() as connection:
        failed_audits = connection.execute(
            select(func.count()).select_from(audit_events).where(
                audit_events.c.request_id.in_(
                    ("remove-last-manager", "suspend-last-manager")
                )
            )
        ).scalar_one()
    assert failed_audits == 0


def test_invited_or_removed_membership_cannot_be_activated_by_status_toggle(
    identity_repository,
) -> None:
    repository, engine = identity_repository
    with engine.begin() as connection:
        connection.execute(
            organization_memberships.update()
            .where(organization_memberships.c.id == "membership-target")
            .values(status="removed")
        )

    with pytest.raises(AdministrationStatusConflictError):
        repository.update_membership_status(
            context=_admin_context(),
            membership_id="membership-target",
            status="active",
            expected_version=1,
            request_id="reactivate-removed",
            occurred_at=NOW,
        )


def test_optimistic_version_conflict_has_no_business_or_audit_side_effect(
    identity_repository,
) -> None:
    repository, engine = identity_repository

    with pytest.raises(AdministrationVersionConflictError):
        repository.replace_membership_facility_scope(
            context=_admin_context(),
            membership_id="membership-target",
            facility_ids=("facility-a",),
            expected_version=9,
            request_id="stale-update",
            occurred_at=NOW,
        )

    with engine.connect() as connection:
        assert connection.execute(
            select(func.count()).select_from(audit_events).where(
                audit_events.c.request_id == "stale-update"
            )
        ).scalar_one() == 0
        assert connection.execute(
            select(organization_memberships.c.version).where(
                organization_memberships.c.id == "membership-target"
            )
        ).scalar_one() == 1


def test_facility_catalog_sync_is_idempotent_and_records_only_aggregate_counts(
    identity_repository,
) -> None:
    repository, engine = identity_repository
    inpatient = build_sqlalchemy_table("inpatient")
    with engine.begin() as connection:
        connection.execute(
            inpatient.insert(),
            [
                {"PermanentFacilityId": "FACILITY-A", "FacilityName": "Facility A"},
                {"PermanentFacilityId": "FACILITY-C", "FacilityName": "Facility C"},
                {"PermanentFacilityId": None, "FacilityName": "Unknown"},
            ],
        )

    first = repository.sync_organization_facilities(
        context=_admin_context(),
        request_id="sync-first",
        occurred_at=NOW,
    )
    second = repository.sync_organization_facilities(
        context=_admin_context(),
        request_id="sync-second",
        occurred_at=NOW + timedelta(minutes=1),
    )

    assert (first.created_count, first.existing_count) == (1, 1)
    assert (second.created_count, second.existing_count) == (0, 2)
    with engine.connect() as connection:
        keys = set(
            connection.execute(
                select(facilities.c.facility_key)
                .select_from(
                    facilities.join(
                        organization_facilities,
                        organization_facilities.c.facility_id == facilities.c.id,
                    )
                )
                .where(organization_facilities.c.organization_id == "organization-1")
            ).scalars()
        )
        details = connection.execute(
            select(audit_events.c.details)
            .where(audit_events.c.request_id.in_(("sync-first", "sync-second")))
            .order_by(audit_events.c.request_id)
        ).scalars().all()
    assert {"FACILITY-A", "FACILITY-C"}.issubset(keys)
    assert details == [
        {"created_count": 1, "existing_count": 1},
        {"created_count": 0, "existing_count": 2},
    ]


def test_facility_catalog_sync_fails_closed_on_foreign_ownership(
    identity_repository,
) -> None:
    repository, engine = identity_repository
    inpatient = build_sqlalchemy_table("inpatient")
    with engine.begin() as connection:
        connection.execute(
            inpatient.insert(),
            [
                {
                    "PermanentFacilityId": "FACILITY-OTHER-ORG",
                    "FacilityName": "Other Organization Facility",
                },
                {"PermanentFacilityId": "FACILITY-NEW", "FacilityName": "New Facility"},
            ],
        )

    with pytest.raises(FacilityOwnershipConflictError):
        repository.sync_organization_facilities(
            context=_admin_context(),
            request_id="sync-conflict",
            occurred_at=NOW,
        )

    with engine.connect() as connection:
        assert connection.execute(
            select(func.count()).select_from(facilities).where(
                facilities.c.facility_key == "FACILITY-NEW"
            )
        ).scalar_one() == 0
        assert connection.execute(
            select(func.count()).select_from(audit_events).where(
                audit_events.c.request_id == "sync-conflict"
            )
        ).scalar_one() == 0


def test_facility_catalog_sync_requires_explicit_dataset_tenant_binding(
    identity_repository,
) -> None:
    repository, engine = identity_repository
    repository.settings = Settings(
        _env_file=None,
        inpatient_dataset_owner_organization_id="organization-2",
    )

    with pytest.raises(FacilityCatalogScopeUnavailableError):
        repository.sync_organization_facilities(
            context=_admin_context(),
            request_id="sync-wrong-owner",
            occurred_at=NOW,
        )

    with engine.connect() as connection:
        assert connection.execute(
            select(func.count()).select_from(audit_events).where(
                audit_events.c.request_id == "sync-wrong-owner"
            )
        ).scalar_one() == 0


def _seed_identity_data(engine) -> None:
    with engine.begin() as connection:
        connection.execute(
            users.insert(),
            [
                {
                    "id": "user-1",
                    "email": "analyst@example.invalid",
                    "email_normalized": "analyst@example.invalid",
                    "display_name": "Analyst",
                    "status": "active",
                },
                {
                    "id": "user-target",
                    "email": "target@example.invalid",
                    "email_normalized": "target@example.invalid",
                    "display_name": "Target Member",
                    "status": "active",
                },
            ],
        )
        connection.execute(
            organizations.insert(),
            [
                {
                    "id": "organization-1",
                    "name": "Hospital One",
                    "slug": "hospital-one",
                    "status": "active",
                },
                {
                    "id": "organization-2",
                    "name": "Hospital Two",
                    "slug": "hospital-two",
                    "status": "active",
                },
                {
                    "id": "organization-empty",
                    "name": "Hospital Empty",
                    "slug": "hospital-empty",
                    "status": "active",
                },
            ],
        )
        connection.execute(
            organization_memberships.insert(),
            [
                {
                    "id": "membership-1",
                    "organization_id": "organization-1",
                    "user_id": "user-1",
                    "status": "active",
                },
                {
                    "id": "membership-2",
                    "organization_id": "organization-2",
                    "user_id": "user-1",
                    "status": "active",
                },
                {
                    "id": "membership-empty",
                    "organization_id": "organization-empty",
                    "user_id": "user-1",
                    "status": "active",
                },
                {
                    "id": "membership-target",
                    "organization_id": "organization-1",
                    "user_id": "user-target",
                    "status": "active",
                },
            ],
        )
        connection.execute(
            permissions.insert(),
            [
                {
                    "id": "permission-query",
                    "permission_key": PermissionCode.ANALYTICS_QUERY_EXECUTE.value,
                    "resource": "analytics.query",
                    "action": "execute",
                },
                {
                    "id": "permission-schema",
                    "permission_key": PermissionCode.ANALYTICS_SCHEMA_READ.value,
                    "resource": "analytics.schema",
                    "action": "read",
                },
                {
                    "id": "permission-future",
                    "permission_key": "analytics.future.capability",
                    "resource": "analytics.future",
                    "action": "capability",
                },
                {
                    "id": "permission-users-manage",
                    "permission_key": PermissionCode.USERS_MANAGE.value,
                    "resource": "users",
                    "action": "manage",
                },
                {
                    "id": "permission-roles-assign",
                    "permission_key": PermissionCode.ROLES_ASSIGN.value,
                    "resource": "roles",
                    "action": "assign",
                },
            ],
        )
        connection.execute(
            roles.insert(),
            [
                {
                    "id": "role-1",
                    "organization_id": "organization-1",
                    "role_key": "analyst-primary",
                    "name": "Analyst Primary",
                },
                {
                    "id": "role-2",
                    "organization_id": "organization-1",
                    "role_key": "analyst-secondary",
                    "name": "Analyst Secondary",
                },
                {
                    "id": "role-admin",
                    "organization_id": "organization-1",
                    "role_key": "organization-admin",
                    "name": "Organization Admin",
                },
                {
                    "id": "role-users-manager",
                    "organization_id": "organization-1",
                    "role_key": "users-manager",
                    "name": "Users Manager",
                },
                {
                    "id": "role-roles-manager",
                    "organization_id": "organization-1",
                    "role_key": "roles-manager",
                    "name": "Roles Manager",
                },
                {
                    "id": "role-other-org",
                    "organization_id": "organization-2",
                    "role_key": "other-analyst",
                    "name": "Other Organization Analyst",
                },
            ],
        )
        connection.execute(
            membership_roles.insert(),
            [
                {
                    "organization_id": "organization-1",
                    "membership_id": "membership-1",
                    "role_id": "role-1",
                },
                {
                    "organization_id": "organization-1",
                    "membership_id": "membership-1",
                    "role_id": "role-2",
                },
                {
                    "organization_id": "organization-1",
                    "membership_id": "membership-1",
                    "role_id": "role-admin",
                },
            ],
        )
        connection.execute(
            role_permissions.insert(),
            [
                {"role_id": "role-1", "permission_id": "permission-query"},
                {"role_id": "role-1", "permission_id": "permission-future"},
                {"role_id": "role-2", "permission_id": "permission-query"},
                {"role_id": "role-2", "permission_id": "permission-schema"},
                {"role_id": "role-admin", "permission_id": "permission-users-manage"},
                {"role_id": "role-admin", "permission_id": "permission-roles-assign"},
                {
                    "role_id": "role-users-manager",
                    "permission_id": "permission-users-manage",
                },
                {
                    "role_id": "role-roles-manager",
                    "permission_id": "permission-roles-assign",
                },
            ],
        )
        connection.execute(
            facilities.insert(),
            [
                {"id": "facility-a", "facility_key": "FACILITY-A", "status": "active"},
                {"id": "facility-b", "facility_key": "FACILITY-B", "status": "active"},
                {
                    "id": "facility-disabled",
                    "facility_key": "FACILITY-DISABLED",
                    "status": "disabled",
                },
                {
                    "id": "facility-other-org",
                    "facility_key": "FACILITY-OTHER-ORG",
                    "status": "active",
                },
            ],
        )
        connection.execute(
            organization_facilities.insert(),
            [
                {"organization_id": "organization-1", "facility_id": "facility-a"},
                {"organization_id": "organization-1", "facility_id": "facility-b"},
                {
                    "organization_id": "organization-1",
                    "facility_id": "facility-disabled",
                },
                {
                    "organization_id": "organization-2",
                    "facility_id": "facility-other-org",
                },
            ],
        )
        connection.execute(
            membership_facility_scopes.insert(),
            [
                {
                    "membership_id": "membership-1",
                    "facility_id": "facility-a",
                    "organization_id": "organization-1",
                },
                {
                    "membership_id": "membership-1",
                    "facility_id": "facility-b",
                    "organization_id": "organization-1",
                },
                {
                    "membership_id": "membership-1",
                    "facility_id": "facility-disabled",
                    "organization_id": "organization-1",
                },
                {
                    "membership_id": "membership-2",
                    "facility_id": "facility-other-org",
                    "organization_id": "organization-2",
                },
            ],
        )
        connection.execute(
            auth_sessions.insert(),
            [
                {
                    "id": "session-1",
                    "membership_id": "membership-1",
                    "user_id": "user-1",
                    "organization_id": "organization-1",
                    "session_token_hash": b"s" * 32,
                    "csrf_token_hash": b"c" * 32,
                    "expires_at": NOW + timedelta(hours=1),
                    "idle_expires_at": NOW + timedelta(minutes=30),
                    "identity_version": 1,
                    "authorization_version": 1,
                },
                {
                    "id": "session-other",
                    "membership_id": "membership-1",
                    "user_id": "user-1",
                    "organization_id": "organization-1",
                    "session_token_hash": b"o" * 32,
                    "csrf_token_hash": b"d" * 32,
                    "expires_at": NOW + timedelta(hours=1),
                    "idle_expires_at": NOW + timedelta(minutes=30),
                    "identity_version": 1,
                    "authorization_version": 1,
                },
            ],
        )
