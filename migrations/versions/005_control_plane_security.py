"""Reconcile tenant integrity and immutable audit actor provenance.

Revision ID: 005_control_plane_security
Revises: 004_facility_scope_index
"""

from __future__ import annotations

from pathlib import Path

from alembic import context, op
from sqlalchemy import CHAR, Column, String, inspect, text
from sqlalchemy.engine import Connection

from medical_ai.identity.schema import identity_metadata

revision = "005_control_plane_security"
down_revision = "004_facility_scope_index"
branch_labels = None
depends_on = None

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SQL_MIGRATIONS = PROJECT_ROOT / "infra" / "mysql" / "migrations"

_UP_SQL = "005_control_plane_security.up.sql"
_DOWN_SQL = "005_control_plane_security.down.sql"

_CREATED_AT_TABLES = (
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
    "facilities",
    "organization_facilities",
    "membership_facility_scopes",
)
_UPDATED_AT_TABLES = (
    "users",
    "organizations",
    "organization_memberships",
    "roles",
    "permissions",
    "auth_sessions",
    "background_jobs",
    "dataset_versions",
    "facilities",
    "organization_facilities",
    "membership_facility_scopes",
)
_LEGACY_NO_COMMENT_IDS = (
    "users",
    "organizations",
    "organization_memberships",
    "roles",
    "permissions",
    "background_jobs",
    "dataset_versions",
    "facilities",
)


def _legacy_column_comments() -> dict[tuple[str, str], str | None]:
    """Return the exact comments deployed by immutable revisions 002/003."""

    comments: dict[tuple[str, str], str | None] = {}
    for table_name in _CREATED_AT_TABLES:
        comments[(table_name, "created_at")] = "UTC"
        comments[(table_name, "version")] = None
    for table_name in _UPDATED_AT_TABLES:
        comments[(table_name, "updated_at")] = "Application-managed UTC"
    for table_name in _LEGACY_NO_COMMENT_IDS:
        comments[(table_name, "id")] = None

    comments.update(
        {
            ("users", "password_hash"): "Password verifier; never plaintext",
            ("users", "auth_version"): None,
            ("users", "email_verified_at"): "UTC",
            ("users", "last_login_at"): "UTC",
            ("users", "deleted_at"): "UTC",
            ("organizations", "deleted_at"): "UTC",
            ("organization_memberships", "authorization_version"): None,
            ("organization_memberships", "joined_at"): "UTC",
            ("membership_roles", "assigned_at"): "UTC",
            ("role_permissions", "granted_at"): "UTC",
            ("auth_sessions", "id"): "Non-secret session identifier",
            ("auth_sessions", "session_token_hash"): (
                "SHA-256 or stronger digest; never plaintext"
            ),
            ("auth_sessions", "refresh_token_hash"): (
                "SHA-256 or stronger digest; never plaintext"
            ),
            ("auth_sessions", "csrf_token_hash"): (
                "SHA-256 or stronger digest; never plaintext"
            ),
            ("auth_sessions", "expires_at"): "UTC",
            ("auth_sessions", "idle_expires_at"): "UTC",
            ("auth_sessions", "last_seen_at"): "UTC",
            ("auth_sessions", "revoked_at"): "UTC",
            ("auth_sessions", "identity_version"): None,
            ("auth_sessions", "authorization_version"): None,
            ("one_time_tokens", "id"): "Non-secret token record identifier",
            ("one_time_tokens", "token_hash"): (
                "SHA-256 or stronger digest; never plaintext"
            ),
            ("one_time_tokens", "expires_at"): "UTC",
            ("one_time_tokens", "consumed_at"): "UTC",
            ("audit_events", "occurred_at"): "UTC",
            ("audit_events", "details"): "Redacted aggregate metadata only",
            ("background_jobs", "input_payload"): (
                "Validated and redacted input only"
            ),
            ("background_jobs", "result_payload"): (
                "Aggregate metadata or artifact references only"
            ),
            ("background_jobs", "lease_token_hash"): (
                "Worker lease digest; never plaintext"
            ),
            ("background_jobs", "queued_at"): "UTC",
            ("background_jobs", "started_at"): "UTC",
            ("background_jobs", "heartbeat_at"): "UTC",
            ("background_jobs", "completed_at"): "UTC",
            ("dataset_versions", "activated_at"): "UTC",
            ("facilities", "facility_key"): (
                "Matches analytics PermanentFacilityId"
            ),
            ("organization_facilities", "granted_at"): "UTC",
            ("membership_facility_scopes", "organization_id"): (
                "Denormalized tenant key for composite foreign keys"
            ),
            ("membership_facility_scopes", "granted_at"): "UTC",
        }
    )
    return comments


LEGACY_COLUMN_COMMENTS = _legacy_column_comments()
LEGACY_SCOPE_TABLE_COMMENT = (
    "Explicit grants; no rows means no facility access, never unrestricted access"
)


def upgrade() -> None:
    """Apply fail-closed tenant constraints after validating legacy rows."""

    if context.is_offline_mode():
        _execute_sql_file(_UP_SQL)
    else:
        connection = op.get_bind()
        _validate_legacy_rows(connection)
        _upgrade_background_job_scope()
        _upgrade_facility_ownership()
        _upgrade_dataset_job_scope()
        _upgrade_audit_actor_scope()
    _reconcile_comments(use_metadata_comments=True)


def downgrade() -> None:
    """Restore the exact 004 schema without deleting audit event rows."""

    _reconcile_comments(use_metadata_comments=False)
    if context.is_offline_mode():
        _execute_sql_file(_DOWN_SQL)
    else:
        _downgrade_dataset_job_scope()
        _downgrade_background_job_scope()
        _downgrade_facility_ownership()
        _downgrade_audit_actor_scope()


def _validate_legacy_rows(connection: Connection) -> None:
    """Abort before MySQL auto-commits DDL if existing tenant facts are unsafe."""

    cross_tenant_jobs = connection.execute(
        text(
            """
            SELECT COUNT(*)
            FROM dataset_versions AS dv
            LEFT JOIN background_jobs AS bj ON bj.id = dv.import_job_id
            WHERE dv.import_job_id IS NOT NULL
              AND (bj.id IS NULL OR bj.organization_id <> dv.organization_id)
            """
        )
    ).scalar_one()
    if cross_tenant_jobs:
        raise RuntimeError(
            "005 preflight failed: dataset import jobs cross organization boundaries"
        )

    untrusted_user_actors = connection.execute(
        text(
            """
            SELECT COUNT(*)
            FROM audit_events AS ae
            LEFT JOIN organization_memberships AS om
              ON om.user_id = ae.actor_user_id
             AND om.organization_id = ae.organization_id
            WHERE ae.actor_user_id IS NOT NULL
              AND (ae.organization_id IS NULL OR om.id IS NULL)
            """
        )
    ).scalar_one()
    if untrusted_user_actors:
        raise RuntimeError(
            "005 preflight failed: audit user actors lack a trusted membership context"
        )

    multiply_owned_facilities = connection.execute(
        text(
            """
            SELECT COUNT(*)
            FROM (
              SELECT facility_id
              FROM organization_facilities
              GROUP BY facility_id
              HAVING COUNT(DISTINCT organization_id) > 1
            ) AS duplicate_owners
            """
        )
    ).scalar_one()
    if multiply_owned_facilities:
        raise RuntimeError(
            "005 preflight failed: facilities have multiple organization owners"
        )


def _upgrade_background_job_scope() -> None:
    if not _unique_exists(
        "background_jobs",
        "uq_background_jobs_id_organization",
    ):
        op.create_unique_constraint(
            "uq_background_jobs_id_organization",
            "background_jobs",
            ["id", "organization_id"],
        )


def _upgrade_facility_ownership() -> None:
    if not _unique_exists(
        "organization_facilities",
        "uq_organization_facilities_facility",
    ):
        op.create_unique_constraint(
            "uq_organization_facilities_facility",
            "organization_facilities",
            ["facility_id"],
        )
    if _index_exists(
        "organization_facilities",
        "ix_organization_facilities_facility",
    ):
        op.drop_index(
            "ix_organization_facilities_facility",
            table_name="organization_facilities",
        )


def _upgrade_dataset_job_scope() -> None:
    if _foreign_key_exists("dataset_versions", "fk_dataset_versions_import_job"):
        op.drop_constraint(
            "fk_dataset_versions_import_job",
            "dataset_versions",
            type_="foreignkey",
        )
    _ensure_index(
        "dataset_versions",
        "ix_dataset_versions_import_job",
        ["import_job_id", "organization_id"],
    )
    if not _foreign_key_exists(
        "dataset_versions",
        "fk_dataset_versions_import_job_org",
    ):
        op.create_foreign_key(
            "fk_dataset_versions_import_job_org",
            "dataset_versions",
            "background_jobs",
            ["import_job_id", "organization_id"],
            ["id", "organization_id"],
            ondelete="RESTRICT",
        )


def _upgrade_audit_actor_scope() -> None:
    columns = _column_map("audit_events")
    if "actor_kind" not in columns:
        op.add_column(
            "audit_events",
            Column(
                "actor_kind",
                String(16),
                nullable=True,
                comment=(
                    "Actor provenance: a trusted membership user or a system process."
                ),
            ),
        )
    if "actor_membership_id" not in columns:
        op.add_column(
            "audit_events",
            Column(
                "actor_membership_id",
                CHAR(36),
                nullable=True,
                comment=(
                    "Trusted organization membership for user actors; "
                    "NULL for system actors."
                ),
            ),
        )

    op.execute(
        """
        UPDATE audit_events AS ae
        INNER JOIN organization_memberships AS om
          ON om.user_id = ae.actor_user_id
         AND om.organization_id = ae.organization_id
        SET ae.actor_kind = 'user', ae.actor_membership_id = om.id
        WHERE ae.actor_user_id IS NOT NULL
        """
    )
    op.execute(
        """
        UPDATE audit_events
        SET actor_kind = 'system', actor_membership_id = NULL
        WHERE actor_user_id IS NULL
        """
    )

    columns = _column_map("audit_events")
    if columns["actor_kind"].get("nullable", True):
        op.alter_column(
            "audit_events",
            "actor_kind",
            existing_type=String(16),
            existing_nullable=True,
            nullable=False,
            existing_comment=columns["actor_kind"].get("comment"),
            comment=(
                "Actor provenance: a trusted membership user or a system process."
            ),
        )

    if _foreign_key_exists("audit_events", "fk_audit_events_actor_user"):
        op.drop_constraint(
            "fk_audit_events_actor_user",
            "audit_events",
            type_="foreignkey",
        )
    if _foreign_key_exists("audit_events", "fk_audit_events_organization"):
        op.drop_constraint(
            "fk_audit_events_organization",
            "audit_events",
            type_="foreignkey",
        )

    _ensure_index(
        "audit_events",
        "ix_audit_events_actor_context",
        ["actor_membership_id", "actor_user_id", "organization_id"],
    )
    _ensure_index(
        "audit_events",
        "ix_audit_events_membership_occurred",
        ["actor_membership_id", "occurred_at"],
    )

    if not _foreign_key_exists(
        "audit_events",
        "fk_audit_events_organization",
    ):
        op.create_foreign_key(
            "fk_audit_events_organization",
            "audit_events",
            "organizations",
            ["organization_id"],
            ["id"],
            ondelete="RESTRICT",
        )
    if not _foreign_key_exists(
        "audit_events",
        "fk_audit_events_actor_membership_context",
    ):
        op.create_foreign_key(
            "fk_audit_events_actor_membership_context",
            "audit_events",
            "organization_memberships",
            ["actor_membership_id", "actor_user_id", "organization_id"],
            ["id", "user_id", "organization_id"],
            ondelete="RESTRICT",
        )
    if not _check_exists("audit_events", "ck_audit_events_actor_kind"):
        op.create_check_constraint(
            "ck_audit_events_actor_kind",
            "audit_events",
            "actor_kind IN ('user', 'system')",
        )
    if not _check_exists("audit_events", "ck_audit_events_actor_context"):
        op.create_check_constraint(
            "ck_audit_events_actor_context",
            "audit_events",
            "(actor_kind = 'user' "
            "AND actor_membership_id IS NOT NULL "
            "AND actor_user_id IS NOT NULL "
            "AND organization_id IS NOT NULL) "
            "OR (actor_kind = 'system' "
            "AND actor_membership_id IS NULL "
            "AND actor_user_id IS NULL)",
        )


def _downgrade_dataset_job_scope() -> None:
    if _foreign_key_exists(
        "dataset_versions",
        "fk_dataset_versions_import_job_org",
    ):
        op.drop_constraint(
            "fk_dataset_versions_import_job_org",
            "dataset_versions",
            type_="foreignkey",
        )
    _ensure_index(
        "dataset_versions",
        "ix_dataset_versions_import_job",
        ["import_job_id"],
    )
    if not _foreign_key_exists("dataset_versions", "fk_dataset_versions_import_job"):
        op.create_foreign_key(
            "fk_dataset_versions_import_job",
            "dataset_versions",
            "background_jobs",
            ["import_job_id"],
            ["id"],
            ondelete="SET NULL",
        )


def _downgrade_background_job_scope() -> None:
    if _unique_exists("background_jobs", "uq_background_jobs_id_organization"):
        op.drop_constraint(
            "uq_background_jobs_id_organization",
            "background_jobs",
            type_="unique",
        )


def _downgrade_facility_ownership() -> None:
    if not _index_exists(
        "organization_facilities",
        "ix_organization_facilities_facility",
    ):
        op.create_index(
            "ix_organization_facilities_facility",
            "organization_facilities",
            ["facility_id"],
            unique=False,
        )
    if _unique_exists(
        "organization_facilities",
        "uq_organization_facilities_facility",
    ):
        op.drop_constraint(
            "uq_organization_facilities_facility",
            "organization_facilities",
            type_="unique",
        )


def _downgrade_audit_actor_scope() -> None:
    for check_name in (
        "ck_audit_events_actor_context",
        "ck_audit_events_actor_kind",
    ):
        if _check_exists("audit_events", check_name):
            op.drop_constraint(check_name, "audit_events", type_="check")
    for foreign_key_name in (
        "fk_audit_events_actor_membership_context",
        "fk_audit_events_organization",
    ):
        if _foreign_key_exists("audit_events", foreign_key_name):
            op.drop_constraint(
                foreign_key_name,
                "audit_events",
                type_="foreignkey",
            )
    for index_name in (
        "ix_audit_events_membership_occurred",
        "ix_audit_events_actor_context",
    ):
        if _index_exists("audit_events", index_name):
            op.drop_index(index_name, table_name="audit_events")

    if not _foreign_key_exists("audit_events", "fk_audit_events_organization"):
        op.create_foreign_key(
            "fk_audit_events_organization",
            "audit_events",
            "organizations",
            ["organization_id"],
            ["id"],
            ondelete="SET NULL",
        )
    if not _foreign_key_exists("audit_events", "fk_audit_events_actor_user"):
        op.create_foreign_key(
            "fk_audit_events_actor_user",
            "audit_events",
            "users",
            ["actor_user_id"],
            ["id"],
            ondelete="SET NULL",
        )

    columns = _column_map("audit_events")
    if "actor_membership_id" in columns:
        op.drop_column("audit_events", "actor_membership_id")
    if "actor_kind" in columns:
        op.drop_column("audit_events", "actor_kind")


def _reconcile_comments(*, use_metadata_comments: bool) -> None:
    """Explicitly converge every 002/003 comment drift and make it reversible."""

    for (table_name, column_name), legacy_comment in sorted(
        LEGACY_COLUMN_COMMENTS.items()
    ):
        column = identity_metadata.tables[table_name].c[column_name]
        target_comment = column.comment if use_metadata_comments else legacy_comment
        existing_comment = legacy_comment if use_metadata_comments else column.comment
        op.alter_column(
            table_name,
            column_name,
            existing_type=column.type,
            existing_nullable=column.nullable,
            existing_server_default=_mysql_server_default(column),
            existing_comment=existing_comment,
            comment=target_comment,
        )

    table = identity_metadata.tables["membership_facility_scopes"]
    target_table_comment = (
        table.comment if use_metadata_comments else LEGACY_SCOPE_TABLE_COMMENT
    )
    existing_table_comment = (
        LEGACY_SCOPE_TABLE_COMMENT if use_metadata_comments else table.comment
    )
    op.create_table_comment(
        "membership_facility_scopes",
        target_table_comment,
        existing_comment=existing_table_comment,
    )


def _mysql_server_default(column: Column) -> object | None:
    """Preserve MySQL's required parentheses around expression defaults."""

    if column.server_default is None:
        return None
    default = column.server_default.arg
    if str(default).upper() == "UTC_TIMESTAMP(6)":
        return text("(UTC_TIMESTAMP(6))")
    return default


def _ensure_index(table_name: str, index_name: str, columns: list[str]) -> None:
    indexes = {item.get("name"): item for item in inspect(op.get_bind()).get_indexes(table_name)}
    current = indexes.get(index_name)
    if current is not None and current.get("column_names") != columns:
        op.drop_index(index_name, table_name=table_name)
        current = None
    if current is None:
        op.create_index(index_name, table_name, columns, unique=False)


def _column_map(table_name: str) -> dict[str, dict[str, object]]:
    return {
        str(column["name"]): column
        for column in inspect(op.get_bind()).get_columns(table_name)
    }


def _foreign_key_exists(table_name: str, constraint_name: str) -> bool:
    return any(
        item.get("name") == constraint_name
        for item in inspect(op.get_bind()).get_foreign_keys(table_name)
    )


def _unique_exists(table_name: str, constraint_name: str) -> bool:
    return any(
        item.get("name") == constraint_name
        for item in inspect(op.get_bind()).get_unique_constraints(table_name)
    )


def _check_exists(table_name: str, constraint_name: str) -> bool:
    return any(
        item.get("name") == constraint_name
        for item in inspect(op.get_bind()).get_check_constraints(table_name)
    )


def _index_exists(table_name: str, index_name: str) -> bool:
    return any(
        item.get("name") == index_name
        for item in inspect(op.get_bind()).get_indexes(table_name)
    )


def _execute_sql_file(filename: str) -> None:
    sql = (SQL_MIGRATIONS / filename).read_text(encoding="utf-8")
    without_line_comments = "\n".join(
        line for line in sql.splitlines() if not line.lstrip().startswith("--")
    )
    for statement in _split_sql_statements(without_line_comments):
        op.execute(statement)


def _split_sql_statements(sql: str) -> list[str]:
    """Split MySQL DDL at semicolons outside quoted strings."""

    statements: list[str] = []
    current: list[str] = []
    quote: str | None = None
    escaped = False
    index = 0
    while index < len(sql):
        character = sql[index]
        if quote is not None:
            current.append(character)
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                if index + 1 < len(sql) and sql[index + 1] == quote:
                    current.append(sql[index + 1])
                    index += 1
                else:
                    quote = None
        elif character in {"'", '"', "`"}:
            quote = character
            current.append(character)
        elif character == ";":
            statement = "".join(current).strip()
            if statement:
                statements.append(statement)
            current = []
        else:
            current.append(character)
        index += 1

    trailing = "".join(current).strip()
    if trailing:
        statements.append(trailing)
    if quote is not None:
        raise ValueError("Unterminated quoted string in migration SQL")
    return statements
