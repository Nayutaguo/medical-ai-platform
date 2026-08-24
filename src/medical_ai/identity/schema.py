"""SQLAlchemy Core schema for identity, authorization, audit, and job control.

All timestamps are stored as timezone-naive MySQL ``DATETIME(6)`` values whose
semantic timezone is UTC. Callers must convert aware datetimes to UTC before
writing them. Authentication secrets are represented only by cryptographic
hash columns; raw session and one-time token values do not belong in this
schema.
"""

from __future__ import annotations

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CHAR,
    CheckConstraint,
    Column,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    SmallInteger,
    String,
    Table,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.mysql import BINARY, DATETIME


NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

identity_metadata = MetaData(
    naming_convention=NAMING_CONVENTION,
    info={
        "timestamp_policy": "UTC",
        "secret_storage_policy": "hash_only",
        "empty_facility_scope_policy": "deny_all",
    },
)


def _utc_datetime() -> DATETIME:
    """Return the common microsecond-resolution UTC storage type."""

    return DATETIME(fsp=6)


def _created_at_column() -> Column:
    return Column(
        "created_at",
        _utc_datetime(),
        nullable=False,
        server_default=text("UTC_TIMESTAMP(6)"),
        comment="Creation time in UTC.",
    )


def _updated_at_column() -> Column:
    return Column(
        "updated_at",
        _utc_datetime(),
        nullable=False,
        server_default=text("UTC_TIMESTAMP(6)"),
        comment="Last application-managed update time in UTC.",
    )


def _version_column() -> Column:
    return Column(
        "version",
        Integer,
        nullable=False,
        server_default=text("1"),
        comment="Optimistic-lock version; increment on each update.",
    )


TABLE_OPTIONS = {
    "mysql_engine": "InnoDB",
    "mysql_charset": "utf8mb4",
    "mysql_collate": "utf8mb4_0900_ai_ci",
}


users = Table(
    "users",
    identity_metadata,
    Column("id", CHAR(36), primary_key=True, comment="Application-generated UUID."),
    Column("email", String(320), nullable=False),
    Column("email_normalized", String(320), nullable=False),
    Column("password_hash", String(255), nullable=True, comment="Password verifier; never a plaintext password."),
    Column("password_algorithm", String(32), nullable=True),
    Column("display_name", String(120), nullable=False),
    Column("status", String(32), nullable=False, server_default=text("'active'")),
    Column(
        "auth_version",
        Integer,
        nullable=False,
        server_default=text("1"),
        comment="Increment to invalidate every active session for this identity.",
    ),
    Column("email_verified_at", _utc_datetime(), nullable=True, comment="Email verification time in UTC."),
    Column("last_login_at", _utc_datetime(), nullable=True, comment="Most recent successful login time in UTC."),
    _created_at_column(),
    _updated_at_column(),
    Column("deleted_at", _utc_datetime(), nullable=True, comment="Soft-deletion time in UTC."),
    _version_column(),
    UniqueConstraint("email_normalized", name="uq_users_email_normalized"),
    CheckConstraint("status IN ('invited', 'active', 'suspended', 'disabled')", name="ck_users_status"),
    CheckConstraint("auth_version >= 1", name="ck_users_auth_version_positive"),
    CheckConstraint("version >= 1", name="ck_users_version_positive"),
    Index("ix_users_status", "status"),
    **TABLE_OPTIONS,
)


organizations = Table(
    "organizations",
    identity_metadata,
    Column("id", CHAR(36), primary_key=True, comment="Application-generated UUID."),
    Column("name", String(200), nullable=False),
    Column("slug", String(100), nullable=False),
    Column("status", String(32), nullable=False, server_default=text("'active'")),
    _created_at_column(),
    _updated_at_column(),
    Column("deleted_at", _utc_datetime(), nullable=True, comment="Soft-deletion time in UTC."),
    _version_column(),
    UniqueConstraint("slug", name="uq_organizations_slug"),
    CheckConstraint("status IN ('active', 'suspended', 'disabled')", name="ck_organizations_status"),
    CheckConstraint("version >= 1", name="ck_organizations_version_positive"),
    Index("ix_organizations_status", "status"),
    **TABLE_OPTIONS,
)


organization_memberships = Table(
    "organization_memberships",
    identity_metadata,
    Column("id", CHAR(36), primary_key=True, comment="Application-generated UUID."),
    Column(
        "organization_id",
        CHAR(36),
        ForeignKey("organizations.id", name="fk_organization_memberships_organization", ondelete="CASCADE"),
        nullable=False,
    ),
    Column(
        "user_id",
        CHAR(36),
        ForeignKey("users.id", name="fk_organization_memberships_user", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("status", String(32), nullable=False, server_default=text("'active'")),
    Column(
        "authorization_version",
        Integer,
        nullable=False,
        server_default=text("1"),
        comment="Increment after any role or data-scope change.",
    ),
    Column("joined_at", _utc_datetime(), nullable=False, server_default=text("UTC_TIMESTAMP(6)")),
    _created_at_column(),
    _updated_at_column(),
    _version_column(),
    UniqueConstraint("organization_id", "user_id", name="uq_organization_memberships_organization_user"),
    UniqueConstraint("id", "organization_id", name="uq_organization_memberships_id_organization"),
    UniqueConstraint(
        "id",
        "user_id",
        "organization_id",
        name="uq_organization_memberships_session_context",
    ),
    CheckConstraint(
        "status IN ('invited', 'active', 'suspended', 'removed')",
        name="ck_organization_memberships_status",
    ),
    CheckConstraint(
        "authorization_version >= 1",
        name="ck_organization_memberships_authorization_version_positive",
    ),
    CheckConstraint("version >= 1", name="ck_organization_memberships_version_positive"),
    Index("ix_organization_memberships_user_status", "user_id", "status"),
    Index("ix_organization_memberships_organization_status", "organization_id", "status"),
    **TABLE_OPTIONS,
)


roles = Table(
    "roles",
    identity_metadata,
    Column("id", CHAR(36), primary_key=True, comment="Application-generated UUID."),
    Column(
        "organization_id",
        CHAR(36),
        ForeignKey("organizations.id", name="fk_roles_organization", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("role_key", String(100), nullable=False),
    Column("name", String(120), nullable=False),
    Column("description", Text, nullable=True),
    Column("is_system", Boolean, nullable=False, server_default=text("0")),
    _created_at_column(),
    _updated_at_column(),
    _version_column(),
    UniqueConstraint("organization_id", "role_key", name="uq_roles_organization_role_key"),
    UniqueConstraint("id", "organization_id", name="uq_roles_id_organization"),
    CheckConstraint("version >= 1", name="ck_roles_version_positive"),
    Index("ix_roles_organization_system", "organization_id", "is_system"),
    **TABLE_OPTIONS,
)


permissions = Table(
    "permissions",
    identity_metadata,
    Column("id", CHAR(36), primary_key=True, comment="Application-generated UUID."),
    Column("permission_key", String(160), nullable=False),
    Column("resource", String(80), nullable=False),
    Column("action", String(80), nullable=False),
    Column("description", Text, nullable=True),
    _created_at_column(),
    _updated_at_column(),
    _version_column(),
    UniqueConstraint("permission_key", name="uq_permissions_permission_key"),
    UniqueConstraint("resource", "action", name="uq_permissions_resource_action"),
    CheckConstraint("version >= 1", name="ck_permissions_version_positive"),
    **TABLE_OPTIONS,
)


membership_roles = Table(
    "membership_roles",
    identity_metadata,
    Column("organization_id", CHAR(36), nullable=False),
    Column("membership_id", CHAR(36), primary_key=True),
    Column("role_id", CHAR(36), primary_key=True),
    Column(
        "assigned_by_user_id",
        CHAR(36),
        ForeignKey("users.id", name="fk_membership_roles_assigned_by_user", ondelete="SET NULL"),
        nullable=True,
    ),
    Column("assigned_at", _utc_datetime(), nullable=False, server_default=text("UTC_TIMESTAMP(6)")),
    _created_at_column(),
    _version_column(),
    ForeignKeyConstraint(
        ("membership_id", "organization_id"),
        ("organization_memberships.id", "organization_memberships.organization_id"),
        name="fk_membership_roles_membership_organization",
        ondelete="CASCADE",
    ),
    ForeignKeyConstraint(
        ("role_id", "organization_id"),
        ("roles.id", "roles.organization_id"),
        name="fk_membership_roles_role_organization",
        ondelete="CASCADE",
    ),
    CheckConstraint("version >= 1", name="ck_membership_roles_version_positive"),
    Index("ix_membership_roles_organization_role", "organization_id", "role_id"),
    **TABLE_OPTIONS,
)


role_permissions = Table(
    "role_permissions",
    identity_metadata,
    Column(
        "role_id",
        CHAR(36),
        ForeignKey("roles.id", name="fk_role_permissions_role", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "permission_id",
        CHAR(36),
        ForeignKey("permissions.id", name="fk_role_permissions_permission", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "granted_by_user_id",
        CHAR(36),
        ForeignKey("users.id", name="fk_role_permissions_granted_by_user", ondelete="SET NULL"),
        nullable=True,
    ),
    Column("granted_at", _utc_datetime(), nullable=False, server_default=text("UTC_TIMESTAMP(6)")),
    _created_at_column(),
    _version_column(),
    CheckConstraint("version >= 1", name="ck_role_permissions_version_positive"),
    Index("ix_role_permissions_permission", "permission_id"),
    **TABLE_OPTIONS,
)


auth_sessions = Table(
    "auth_sessions",
    identity_metadata,
    Column("id", CHAR(36), primary_key=True, comment="Non-secret application-generated session identifier."),
    Column("membership_id", CHAR(36), nullable=False),
    Column("user_id", CHAR(36), nullable=False),
    Column("organization_id", CHAR(36), nullable=False),
    Column("session_token_hash", BINARY(32), nullable=False, comment="SHA-256 or stronger digest of the session token."),
    Column("refresh_token_hash", BINARY(32), nullable=True, comment="SHA-256 or stronger digest of the refresh token."),
    Column("csrf_token_hash", BINARY(32), nullable=False, comment="SHA-256 or stronger digest of the CSRF token."),
    Column("ip_address_hash", BINARY(32), nullable=True),
    Column("user_agent_hash", BINARY(32), nullable=True),
    Column("expires_at", _utc_datetime(), nullable=False, comment="Absolute expiration time in UTC."),
    Column("idle_expires_at", _utc_datetime(), nullable=False, comment="Idle expiration time in UTC."),
    Column("last_seen_at", _utc_datetime(), nullable=True, comment="Most recent activity time in UTC."),
    Column("revoked_at", _utc_datetime(), nullable=True, comment="Revocation time in UTC."),
    Column(
        "identity_version",
        Integer,
        nullable=False,
        comment="Snapshot of users.auth_version when the session was issued.",
    ),
    Column(
        "authorization_version",
        Integer,
        nullable=False,
        comment="Snapshot of membership authorization_version when issued.",
    ),
    _created_at_column(),
    _updated_at_column(),
    _version_column(),
    ForeignKeyConstraint(
        ("membership_id", "user_id", "organization_id"),
        (
            "organization_memberships.id",
            "organization_memberships.user_id",
            "organization_memberships.organization_id",
        ),
        name="fk_auth_sessions_membership_context",
        ondelete="CASCADE",
    ),
    UniqueConstraint("session_token_hash", name="uq_auth_sessions_session_token_hash"),
    UniqueConstraint("refresh_token_hash", name="uq_auth_sessions_refresh_token_hash"),
    UniqueConstraint("csrf_token_hash", name="uq_auth_sessions_csrf_token_hash"),
    CheckConstraint("identity_version >= 1", name="ck_auth_sessions_identity_version_positive"),
    CheckConstraint("authorization_version >= 1", name="ck_auth_sessions_authorization_version_positive"),
    CheckConstraint("idle_expires_at <= expires_at", name="ck_auth_sessions_idle_before_absolute_expiry"),
    CheckConstraint("version >= 1", name="ck_auth_sessions_version_positive"),
    Index("ix_auth_sessions_membership_expires", "membership_id", "expires_at"),
    Index("ix_auth_sessions_user_expires", "user_id", "expires_at"),
    Index("ix_auth_sessions_organization_expires", "organization_id", "expires_at"),
    Index("ix_auth_sessions_revoked_expires", "revoked_at", "expires_at"),
    **TABLE_OPTIONS,
)


one_time_tokens = Table(
    "one_time_tokens",
    identity_metadata,
    Column("id", CHAR(36), primary_key=True, comment="Non-secret application-generated token record identifier."),
    Column("organization_id", CHAR(36), nullable=False),
    Column("membership_id", CHAR(36), nullable=False),
    Column("user_id", CHAR(36), nullable=False),
    Column("purpose", String(48), nullable=False),
    Column("token_hash", BINARY(32), nullable=False, comment="SHA-256 or stronger digest; raw token is never stored."),
    Column(
        "identity_version",
        Integer,
        nullable=False,
        comment="Snapshot of users.auth_version when the token was issued.",
    ),
    Column("expires_at", _utc_datetime(), nullable=False, comment="Expiration time in UTC."),
    Column("consumed_at", _utc_datetime(), nullable=True, comment="Consumption time in UTC."),
    _created_at_column(),
    _version_column(),
    ForeignKeyConstraint(
        ("membership_id", "user_id", "organization_id"),
        (
            "organization_memberships.id",
            "organization_memberships.user_id",
            "organization_memberships.organization_id",
        ),
        name="fk_one_time_tokens_membership_context",
        ondelete="CASCADE",
    ),
    UniqueConstraint("token_hash", name="uq_one_time_tokens_token_hash"),
    CheckConstraint(
        "purpose = 'user_invitation'",
        name="ck_one_time_tokens_invitation_purpose",
    ),
    CheckConstraint(
        "identity_version >= 1",
        name="ck_one_time_tokens_identity_version_positive",
    ),
    CheckConstraint("version >= 1", name="ck_one_time_tokens_version_positive"),
    Index("ix_one_time_tokens_user_purpose_expires", "user_id", "purpose", "expires_at"),
    Index(
        "ix_one_time_tokens_organization_purpose_expires",
        "organization_id",
        "purpose",
        "expires_at",
    ),
    Index("ix_one_time_tokens_expires_consumed", "expires_at", "consumed_at"),
    **TABLE_OPTIONS,
)


audit_events = Table(
    "audit_events",
    identity_metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("occurred_at", _utc_datetime(), nullable=False, server_default=text("UTC_TIMESTAMP(6)")),
    Column("request_id", String(128), nullable=True),
    Column(
        "organization_id",
        CHAR(36),
        nullable=True,
    ),
    Column(
        "actor_kind",
        String(16),
        nullable=False,
        comment="Actor provenance: a trusted membership user or a system process.",
    ),
    Column(
        "actor_membership_id",
        CHAR(36),
        nullable=True,
        comment="Trusted organization membership for user actors; NULL for system actors.",
    ),
    Column(
        "actor_user_id",
        CHAR(36),
        nullable=True,
    ),
    Column("action", String(120), nullable=False),
    Column("resource_type", String(80), nullable=False),
    Column("resource_id", String(128), nullable=True),
    Column("outcome", String(32), nullable=False),
    Column("error_code", String(80), nullable=True),
    Column("details", JSON, nullable=True, comment="Redacted aggregate metadata only; never credentials or patient rows."),
    Column("ip_address_hash", BINARY(32), nullable=True),
    Column("user_agent_hash", BINARY(32), nullable=True),
    _created_at_column(),
    _version_column(),
    ForeignKeyConstraint(
        ("organization_id",),
        ("organizations.id",),
        name="fk_audit_events_organization",
        ondelete="RESTRICT",
    ),
    ForeignKeyConstraint(
        ("actor_membership_id", "actor_user_id", "organization_id"),
        (
            "organization_memberships.id",
            "organization_memberships.user_id",
            "organization_memberships.organization_id",
        ),
        name="fk_audit_events_actor_membership_context",
        ondelete="RESTRICT",
    ),
    CheckConstraint(
        "actor_kind IN ('user', 'system')",
        name="ck_audit_events_actor_kind",
    ),
    CheckConstraint(
        "(actor_kind = 'user' "
        "AND actor_membership_id IS NOT NULL "
        "AND actor_user_id IS NOT NULL "
        "AND organization_id IS NOT NULL) "
        "OR (actor_kind = 'system' "
        "AND actor_membership_id IS NULL "
        "AND actor_user_id IS NULL)",
        name="ck_audit_events_actor_context",
    ),
    CheckConstraint("outcome IN ('success', 'denied', 'failure')", name="ck_audit_events_outcome"),
    CheckConstraint("version >= 1", name="ck_audit_events_version_positive"),
    Index("ix_audit_events_request", "request_id"),
    Index("ix_audit_events_organization_occurred", "organization_id", "occurred_at"),
    Index("ix_audit_events_actor_occurred", "actor_user_id", "occurred_at"),
    Index(
        "ix_audit_events_actor_context",
        "actor_membership_id",
        "actor_user_id",
        "organization_id",
    ),
    Index(
        "ix_audit_events_membership_occurred",
        "actor_membership_id",
        "occurred_at",
    ),
    Index("ix_audit_events_resource", "resource_type", "resource_id", "occurred_at"),
    **TABLE_OPTIONS,
)


background_jobs = Table(
    "background_jobs",
    identity_metadata,
    Column("id", CHAR(36), primary_key=True, comment="Application-generated UUID."),
    Column(
        "organization_id",
        CHAR(36),
        ForeignKey("organizations.id", name="fk_background_jobs_organization", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column(
        "requested_by_user_id",
        CHAR(36),
        ForeignKey("users.id", name="fk_background_jobs_requested_by_user", ondelete="SET NULL"),
        nullable=True,
    ),
    Column("job_type", String(80), nullable=False),
    Column("status", String(32), nullable=False, server_default=text("'queued'")),
    Column("idempotency_key", String(128), nullable=True),
    Column("input_payload", JSON, nullable=True, comment="Validated and redacted job input only."),
    Column("result_payload", JSON, nullable=True, comment="Aggregate result metadata or artifact references only."),
    Column("error_code", String(80), nullable=True),
    Column("error_message", String(500), nullable=True),
    Column("progress_percent", SmallInteger, nullable=False, server_default=text("0")),
    Column("attempt_count", SmallInteger, nullable=False, server_default=text("0")),
    Column("max_attempts", SmallInteger, nullable=False, server_default=text("3")),
    Column("lease_token_hash", BINARY(32), nullable=True, comment="Worker lease secret digest; never plaintext."),
    Column("queued_at", _utc_datetime(), nullable=False, server_default=text("UTC_TIMESTAMP(6)")),
    Column("started_at", _utc_datetime(), nullable=True),
    Column("heartbeat_at", _utc_datetime(), nullable=True),
    Column("completed_at", _utc_datetime(), nullable=True),
    _created_at_column(),
    _updated_at_column(),
    _version_column(),
    UniqueConstraint("organization_id", "idempotency_key", name="uq_background_jobs_organization_idempotency"),
    UniqueConstraint("id", "organization_id", name="uq_background_jobs_id_organization"),
    CheckConstraint(
        "status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')",
        name="ck_background_jobs_status",
    ),
    CheckConstraint("progress_percent BETWEEN 0 AND 100", name="ck_background_jobs_progress"),
    CheckConstraint("attempt_count >= 0 AND max_attempts >= 1", name="ck_background_jobs_attempts"),
    CheckConstraint("version >= 1", name="ck_background_jobs_version_positive"),
    Index("ix_background_jobs_status_queued", "status", "queued_at"),
    Index("ix_background_jobs_organization_status", "organization_id", "status", "queued_at"),
    Index("ix_background_jobs_requested_by", "requested_by_user_id", "created_at"),
    **TABLE_OPTIONS,
)


dataset_versions = Table(
    "dataset_versions",
    identity_metadata,
    Column("id", CHAR(36), primary_key=True, comment="Application-generated UUID."),
    Column(
        "organization_id",
        CHAR(36),
        ForeignKey("organizations.id", name="fk_dataset_versions_organization", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column("dataset_key", String(120), nullable=False),
    Column("version_number", Integer, nullable=False),
    Column("status", String(32), nullable=False, server_default=text("'staged'")),
    Column("source_file_name", String(512), nullable=False),
    Column("source_sha256", CHAR(64), nullable=False),
    Column("schema_version", String(64), nullable=False),
    Column("row_count", BigInteger, nullable=False, server_default=text("0")),
    Column("rejected_row_count", BigInteger, nullable=False, server_default=text("0")),
    Column("quality_profile", JSON, nullable=True),
    Column(
        "imported_by_user_id",
        CHAR(36),
        ForeignKey("users.id", name="fk_dataset_versions_imported_by_user", ondelete="SET NULL"),
        nullable=True,
    ),
    Column(
        "import_job_id",
        CHAR(36),
        nullable=True,
    ),
    Column("activated_at", _utc_datetime(), nullable=True, comment="Activation time in UTC."),
    _created_at_column(),
    _updated_at_column(),
    _version_column(),
    ForeignKeyConstraint(
        ("import_job_id", "organization_id"),
        ("background_jobs.id", "background_jobs.organization_id"),
        name="fk_dataset_versions_import_job_org",
        ondelete="RESTRICT",
    ),
    UniqueConstraint(
        "organization_id",
        "dataset_key",
        "version_number",
        name="uq_dataset_versions_organization_dataset_version",
    ),
    UniqueConstraint(
        "organization_id",
        "dataset_key",
        "source_sha256",
        name="uq_dataset_versions_organization_dataset_source",
    ),
    CheckConstraint("version_number >= 1", name="ck_dataset_versions_number_positive"),
    CheckConstraint("row_count >= 0 AND rejected_row_count >= 0", name="ck_dataset_versions_row_counts"),
    CheckConstraint("status IN ('staged', 'active', 'superseded', 'failed')", name="ck_dataset_versions_status"),
    CheckConstraint("version >= 1", name="ck_dataset_versions_version_positive"),
    Index("ix_dataset_versions_organization_status", "organization_id", "dataset_key", "status"),
    Index(
        "ix_dataset_versions_import_job",
        "import_job_id",
        "organization_id",
    ),
    **TABLE_OPTIONS,
)


facilities = Table(
    "facilities",
    identity_metadata,
    Column("id", CHAR(36), primary_key=True, comment="Application-generated UUID."),
    Column(
        "facility_key",
        String(128),
        nullable=False,
        comment="Canonical key matching analytics PermanentFacilityId.",
    ),
    Column("display_name", String(200), nullable=True),
    Column("status", String(32), nullable=False, server_default=text("'active'")),
    _created_at_column(),
    _updated_at_column(),
    _version_column(),
    UniqueConstraint("facility_key", name="uq_facilities_facility_key"),
    CheckConstraint("status IN ('active', 'disabled')", name="ck_facilities_status"),
    CheckConstraint("version >= 1", name="ck_facilities_version_positive"),
    Index("ix_facilities_status", "status"),
    **TABLE_OPTIONS,
)


organization_facilities = Table(
    "organization_facilities",
    identity_metadata,
    Column("organization_id", CHAR(36), primary_key=True),
    Column("facility_id", CHAR(36), primary_key=True),
    Column(
        "granted_by_user_id",
        CHAR(36),
        ForeignKey(
            "users.id",
            name="fk_organization_facilities_granted_by_user",
            ondelete="SET NULL",
        ),
        nullable=True,
    ),
    Column(
        "granted_at",
        _utc_datetime(),
        nullable=False,
        server_default=text("UTC_TIMESTAMP(6)"),
        comment="Grant time in UTC.",
    ),
    _created_at_column(),
    _updated_at_column(),
    _version_column(),
    ForeignKeyConstraint(
        ("organization_id",),
        ("organizations.id",),
        name="fk_organization_facilities_organization",
        ondelete="CASCADE",
    ),
    ForeignKeyConstraint(
        ("facility_id",),
        ("facilities.id",),
        name="fk_organization_facilities_facility",
        ondelete="CASCADE",
    ),
    UniqueConstraint(
        "facility_id",
        name="uq_organization_facilities_facility",
    ),
    CheckConstraint(
        "version >= 1",
        name="ck_organization_facilities_version_positive",
    ),
    **TABLE_OPTIONS,
)


membership_facility_scopes = Table(
    "membership_facility_scopes",
    identity_metadata,
    Column("membership_id", CHAR(36), primary_key=True),
    Column("facility_id", CHAR(36), primary_key=True),
    Column(
        "organization_id",
        CHAR(36),
        nullable=False,
        comment="Denormalized tenant key used by composite foreign keys.",
    ),
    Column(
        "granted_by_user_id",
        CHAR(36),
        ForeignKey(
            "users.id",
            name="fk_membership_facility_scopes_granted_by_user",
            ondelete="SET NULL",
        ),
        nullable=True,
    ),
    Column(
        "granted_at",
        _utc_datetime(),
        nullable=False,
        server_default=text("UTC_TIMESTAMP(6)"),
        comment="Grant time in UTC.",
    ),
    _created_at_column(),
    _updated_at_column(),
    _version_column(),
    ForeignKeyConstraint(
        ("membership_id", "organization_id"),
        (
            "organization_memberships.id",
            "organization_memberships.organization_id",
        ),
        name="fk_membership_facility_scopes_membership_org",
        ondelete="CASCADE",
    ),
    ForeignKeyConstraint(
        ("organization_id", "facility_id"),
        (
            "organization_facilities.organization_id",
            "organization_facilities.facility_id",
        ),
        name="fk_membership_facility_scopes_org_facility",
        ondelete="CASCADE",
    ),
    CheckConstraint(
        "version >= 1",
        name="ck_membership_facility_scopes_version_positive",
    ),
    Index(
        "ix_membership_facility_scopes_membership_org",
        "membership_id",
        "organization_id",
    ),
    Index(
        "ix_membership_facility_scopes_org_facility",
        "organization_id",
        "facility_id",
    ),
    comment=(
        "Explicit membership grants. No rows means no facility access; it never "
        "means unrestricted access."
    ),
    **TABLE_OPTIONS,
)


BASE_IDENTITY_TABLE_NAMES = (
    "users",
    "organizations",
    "organization_memberships",
    "roles",
    "permissions",
    "membership_roles",
    "role_permissions",
    "auth_sessions",
    "one_time_tokens",
    "audit_events",
    "background_jobs",
    "dataset_versions",
)

SCOPE_IDENTITY_TABLE_NAMES = (
    "facilities",
    "organization_facilities",
    "membership_facility_scopes",
)

ALL_IDENTITY_TABLE_NAMES = BASE_IDENTITY_TABLE_NAMES + SCOPE_IDENTITY_TABLE_NAMES

# Descriptive alias retained for callers that refer to the facility-scope
# migration by feature rather than by control-plane layer.
FACILITY_SCOPE_TABLE_NAMES = SCOPE_IDENTITY_TABLE_NAMES

# Backward-compatible name used by Alembic's control-plane include filter and
# integration checks. Migration-specific tests must use the revision-specific
# constants above so the already-deployed 002 contract remains immutable.
IDENTITY_TABLE_NAMES = ALL_IDENTITY_TABLE_NAMES
