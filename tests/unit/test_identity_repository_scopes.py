from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import StaticPool

from medical_ai.authorization import PermissionCode
from medical_ai.config import Settings
from medical_ai.identity import (
    auth_sessions,
    facilities,
    identity_metadata,
    membership_facility_scopes,
    membership_roles,
    organization_facilities,
    organization_memberships,
    organizations,
    permissions,
    role_permissions,
    roles,
    users,
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

    identity_metadata.create_all(engine)
    _seed_identity_data(engine)
    repository = IdentityRepository(
        settings=Settings(_env_file=None),
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
        }
    )
    assert session.access_context.allowed_facility_ids == frozenset(
        {"FACILITY-A", "FACILITY-B"}
    )
    assert "FACILITY-DISABLED" not in session.access_context.allowed_facility_ids


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
                }
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
            ],
        )
        connection.execute(
            role_permissions.insert(),
            [
                {"role_id": "role-1", "permission_id": "permission-query"},
                {"role_id": "role-1", "permission_id": "permission-future"},
                {"role_id": "role-2", "permission_id": "permission-query"},
                {"role_id": "role-2", "permission_id": "permission-schema"},
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
            auth_sessions.insert().values(
                id="session-1",
                membership_id="membership-1",
                user_id="user-1",
                organization_id="organization-1",
                session_token_hash=b"s" * 32,
                csrf_token_hash=b"c" * 32,
                expires_at=NOW + timedelta(hours=1),
                idle_expires_at=NOW + timedelta(minutes=30),
                identity_version=1,
                authorization_version=1,
            )
        )
