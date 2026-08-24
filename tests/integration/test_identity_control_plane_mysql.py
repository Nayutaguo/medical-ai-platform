import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.exc import IntegrityError

from medical_ai.config import get_settings
from medical_ai.identity import IDENTITY_TABLE_NAMES, organizations
from medical_ai.repositories import IdentityRepository

pytestmark = pytest.mark.integration

requires_mysql = pytest.mark.skipif(
    os.getenv("RUN_MYSQL_TESTS") != "1",
    reason="Set RUN_MYSQL_TESTS=1 and configure MySQL to run.",
)


@requires_mysql
def test_control_plane_tables_coexist_with_analytics_data() -> None:
    """Verify the additive migration without reading patient-level fields."""

    settings = get_settings()
    engine = create_engine(settings.mysql_url(), pool_pre_ping=True)
    table_names = set(inspect(engine).get_table_names())

    assert set(IDENTITY_TABLE_NAMES).issubset(table_names)
    assert "inpatient" in table_names
    assert "idx_inpatient_facility_year" in {
        index["name"] for index in inspect(engine).get_indexes("inpatient")
    }
    with engine.connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM inpatient")).scalar_one() > 0

    repository = IdentityRepository(settings=settings, engine=engine)
    assert repository.find_user_by_normalized_email("missing@example.invalid") is None


@requires_mysql
def test_dataset_import_job_rejects_cross_organization_reference_and_rolls_back() -> None:
    """The composite FK must reject a job owned by a different tenant."""

    engine = _engine()
    organization_a = str(uuid4())
    organization_b = str(uuid4())
    job_id = str(uuid4())
    dataset_id = str(uuid4())

    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            _insert_organization(connection, organization_a, "job-owner-a")
            _insert_organization(connection, organization_b, "dataset-owner-b")
            connection.execute(
                text(
                    """
                    INSERT INTO background_jobs (id, organization_id, job_type)
                    VALUES (:id, :organization_id, 'dataset_import')
                    """
                ),
                {"id": job_id, "organization_id": organization_a},
            )

            with pytest.raises(IntegrityError):
                connection.execute(
                    text(
                        """
                        INSERT INTO dataset_versions (
                          id, organization_id, dataset_key, version_number,
                          source_file_name, source_sha256, schema_version,
                          import_job_id
                        ) VALUES (
                          :id, :organization_id, 'inpatient', 1,
                          'synthetic.csv', :source_sha256, 'test-v1',
                          :import_job_id
                        )
                        """
                    ),
                    {
                        "id": dataset_id,
                        "organization_id": organization_b,
                        "source_sha256": "0" * 64,
                        "import_job_id": job_id,
                    },
                )
        finally:
            transaction.rollback()

    _assert_organizations_rolled_back(engine, organization_a, organization_b)


@requires_mysql
def test_audit_user_actor_rejects_cross_organization_context_and_rolls_back() -> None:
    """Actor membership, user, and organization must describe one tenant context."""

    engine = _engine()
    organization_a = str(uuid4())
    organization_b = str(uuid4())
    user_id = str(uuid4())
    membership_id = str(uuid4())

    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            _insert_organization(connection, organization_a, "actor-owner-a")
            _insert_organization(connection, organization_b, "audit-owner-b")
            connection.execute(
                text(
                    """
                    INSERT INTO users (
                      id, email, email_normalized, display_name
                    ) VALUES (
                      :id, :email, :email, 'Synthetic User'
                    )
                    """
                ),
                {"id": user_id, "email": f"{user_id}@example.invalid"},
            )
            connection.execute(
                text(
                    """
                    INSERT INTO organization_memberships (
                      id, organization_id, user_id
                    ) VALUES (
                      :id, :organization_id, :user_id
                    )
                    """
                ),
                {
                    "id": membership_id,
                    "organization_id": organization_a,
                    "user_id": user_id,
                },
            )

            with pytest.raises(IntegrityError):
                connection.execute(
                    text(
                        """
                        INSERT INTO audit_events (
                          organization_id, actor_kind, actor_membership_id,
                          actor_user_id, action, resource_type, outcome
                        ) VALUES (
                          :organization_id, 'user', :membership_id,
                          :user_id, 'analytics.query', 'dataset', 'denied'
                        )
                        """
                    ),
                    {
                        "organization_id": organization_b,
                        "membership_id": membership_id,
                        "user_id": user_id,
                    },
                )
        finally:
            transaction.rollback()

    _assert_organizations_rolled_back(engine, organization_a, organization_b)


@requires_mysql
def test_facility_cannot_have_two_organization_owners_and_rolls_back() -> None:
    """一期 ownership is exclusive; sharing needs a future explicit grant model."""

    engine = _engine()
    organization_a = str(uuid4())
    organization_b = str(uuid4())
    facility_id = str(uuid4())

    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            _insert_organization(connection, organization_a, "facility-owner-a")
            _insert_organization(connection, organization_b, "facility-owner-b")
            connection.execute(
                text(
                    """
                    INSERT INTO facilities (id, facility_key)
                    VALUES (:id, :facility_key)
                    """
                ),
                {"id": facility_id, "facility_key": f"synthetic-{facility_id}"},
            )
            connection.execute(
                text(
                    """
                    INSERT INTO organization_facilities (organization_id, facility_id)
                    VALUES (:organization_id, :facility_id)
                    """
                ),
                {"organization_id": organization_a, "facility_id": facility_id},
            )

            with pytest.raises(IntegrityError):
                connection.execute(
                    text(
                        """
                        INSERT INTO organization_facilities (organization_id, facility_id)
                        VALUES (:organization_id, :facility_id)
                        """
                    ),
                    {"organization_id": organization_b, "facility_id": facility_id},
                )
        finally:
            transaction.rollback()

    _assert_organizations_rolled_back(engine, organization_a, organization_b)


def _engine():
    settings = get_settings()
    return create_engine(settings.mysql_url(), pool_pre_ping=True)


def _insert_organization(connection, organization_id: str, slug_prefix: str) -> None:
    connection.execute(
        text(
            """
            INSERT INTO organizations (id, name, slug)
            VALUES (:id, 'Synthetic Test Organization', :slug)
            """
        ),
        {"id": organization_id, "slug": f"{slug_prefix}-{organization_id}"},
    )


def _assert_organizations_rolled_back(engine, *organization_ids: str) -> None:
    with engine.connect() as connection:
        remaining = connection.execute(
            select(func.count())
            .select_from(organizations)
            .where(organizations.c.id.in_(organization_ids))
        ).scalar_one()
    assert remaining == 0
