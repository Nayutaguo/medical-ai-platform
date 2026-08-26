"""Persist governed analysis history and enable password-reset token purposes.

Revision ID: 007_demo_completion
Revises: 006_invitation_registration

History rows contain only validated declarative specifications and aggregate
execution metadata. Aggregate result rows, compiled SQL, and raw model output
must never be persisted in this table.
"""

from __future__ import annotations

from alembic import context, op
from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CHAR,
    CheckConstraint,
    Column,
    ForeignKeyConstraint,
    Integer,
    Numeric,
    String,
    inspect,
    text,
)
from sqlalchemy.dialects.mysql import DATETIME


revision = "007_demo_completion"
down_revision = "006_invitation_registration"
branch_labels = None
depends_on = None

HISTORY_TABLE = "analysis_history"
TOKEN_TABLE = "one_time_tokens"
LEGACY_PURPOSE_CHECK = "ck_one_time_tokens_invitation_purpose"
EXPANDED_PURPOSE_CHECK = "ck_one_time_tokens_purpose"

HISTORY_INDEXES: dict[str, list[str]] = {
    "ix_analysis_history_context_id": [
        "membership_id",
        "user_id",
        "organization_id",
        "id",
    ],
    "ix_analysis_history_context_favorite_id": [
        "membership_id",
        "user_id",
        "organization_id",
        "is_favorite",
        "id",
    ],
}


def upgrade() -> None:
    """Add history storage, then safely widen the one-time-token purpose."""

    offline = context.is_offline_mode()
    if offline or not _table_exists(HISTORY_TABLE):
        _create_analysis_history_table()
    for index_name, columns in HISTORY_INDEXES.items():
        _ensure_index(index_name, columns, offline=offline)

    # Add the wider constraint before removing the legacy constraint. If MySQL
    # auto-commits only the first DDL statement, invitation-only enforcement is
    # still present and retrying the migration converges safely.
    if offline or not _check_exists(TOKEN_TABLE, EXPANDED_PURPOSE_CHECK):
        op.create_check_constraint(
            EXPANDED_PURPOSE_CHECK,
            TOKEN_TABLE,
            "purpose IN ('user_invitation', 'password_reset')",
        )
    if offline or _check_exists(TOKEN_TABLE, LEGACY_PURPOSE_CHECK):
        op.drop_constraint(
            LEGACY_PURPOSE_CHECK,
            TOKEN_TABLE,
            type_="check",
        )


def downgrade() -> None:
    """Restore invitation-only tokens without deleting authentication data."""

    offline = context.is_offline_mode()
    if not offline:
        _validate_no_non_invitation_tokens()

    # Re-establish the restrictive check before dropping the wider check. This
    # also makes an offline downgrade fail closed when password-reset rows exist:
    # MySQL rejects the ADD while the expanded constraint remains installed.
    if offline or not _check_exists(TOKEN_TABLE, LEGACY_PURPOSE_CHECK):
        op.create_check_constraint(
            LEGACY_PURPOSE_CHECK,
            TOKEN_TABLE,
            "purpose = 'user_invitation'",
        )
    if offline or _check_exists(TOKEN_TABLE, EXPANDED_PURPOSE_CHECK):
        op.drop_constraint(
            EXPANDED_PURPOSE_CHECK,
            TOKEN_TABLE,
            type_="check",
        )

    # Do not drop user history until the token-schema contraction has succeeded.
    if offline or _table_exists(HISTORY_TABLE):
        op.drop_table(HISTORY_TABLE)


def _create_analysis_history_table() -> None:
    op.create_table(
        HISTORY_TABLE,
        Column("id", BigInteger, primary_key=True, autoincrement=True),
        Column("organization_id", CHAR(36), nullable=False),
        Column("membership_id", CHAR(36), nullable=False),
        Column("user_id", CHAR(36), nullable=False),
        Column(
            "history_type",
            String(16),
            nullable=False,
            comment="Originating governed workflow: query or agent.",
        ),
        Column("title", String(200), nullable=False),
        Column("question", String(2000), nullable=True),
        Column(
            "query_spec",
            JSON,
            nullable=True,
            comment=(
                "Validated QuerySpec only; never aggregate or patient result rows."
            ),
        ),
        Column(
            "chart_spec",
            JSON,
            nullable=True,
            comment="Validated declarative ChartSpec; never executable JavaScript.",
        ),
        Column(
            "row_count",
            BigInteger,
            nullable=False,
            server_default=text("0"),
        ),
        Column(
            "truncated",
            Boolean,
            nullable=False,
            server_default=text("0"),
        ),
        Column(
            "query_time_ms",
            Numeric(12, 3),
            nullable=False,
            server_default=text("0"),
        ),
        Column(
            "is_favorite",
            Boolean,
            nullable=False,
            server_default=text("0"),
        ),
        Column(
            "created_at",
            DATETIME(fsp=6),
            nullable=False,
            server_default=text("(UTC_TIMESTAMP(6))"),
            comment="Creation time in UTC.",
        ),
        Column(
            "updated_at",
            DATETIME(fsp=6),
            nullable=False,
            server_default=text("(UTC_TIMESTAMP(6))"),
            comment="Last application-managed update time in UTC.",
        ),
        Column(
            "version",
            Integer,
            nullable=False,
            server_default=text("1"),
            comment="Optimistic-lock version; increment on each update.",
        ),
        ForeignKeyConstraint(
            ("membership_id", "user_id", "organization_id"),
            (
                "organization_memberships.id",
                "organization_memberships.user_id",
                "organization_memberships.organization_id",
            ),
            name="fk_analysis_history_membership_context",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "history_type IN ('query', 'agent')",
            name="ck_analysis_history_type",
        ),
        CheckConstraint(
            "history_type = 'agent' OR query_spec IS NOT NULL",
            name="ck_analysis_history_query_spec",
        ),
        CheckConstraint(
            "row_count >= 0",
            name="ck_analysis_history_row_count",
        ),
        CheckConstraint(
            "query_time_ms >= 0",
            name="ck_analysis_history_query_time",
        ),
        CheckConstraint(
            "version >= 1",
            name="ck_analysis_history_version_positive",
        ),
        comment=(
            "Per-membership governed analysis history without result-row "
            "persistence."
        ),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_ai_ci",
    )


def _validate_no_non_invitation_tokens() -> None:
    remaining = op.get_bind().execute(
        text(
            "SELECT COUNT(*) FROM one_time_tokens "
            "WHERE purpose <> 'user_invitation'"
        )
    ).scalar_one()
    if remaining:
        raise RuntimeError(
            "007 downgrade preflight failed: password-reset one_time_tokens "
            "must be explicitly archived and removed before restoring the "
            "invitation-only purpose constraint"
        )


def _ensure_index(
    index_name: str,
    columns: list[str],
    *,
    offline: bool,
) -> None:
    if offline:
        op.create_index(index_name, HISTORY_TABLE, columns, unique=False)
        return

    indexes = {
        item.get("name"): item
        for item in inspect(op.get_bind()).get_indexes(HISTORY_TABLE)
    }
    current = indexes.get(index_name)
    if current is not None and current.get("column_names") != columns:
        op.drop_index(index_name, table_name=HISTORY_TABLE)
        current = None
    if current is None:
        op.create_index(index_name, HISTORY_TABLE, columns, unique=False)


def _table_exists(table_name: str) -> bool:
    return inspect(op.get_bind()).has_table(table_name)


def _check_exists(table_name: str, constraint_name: str) -> bool:
    return any(
        item.get("name") == constraint_name
        for item in inspect(op.get_bind()).get_check_constraints(table_name)
    )
