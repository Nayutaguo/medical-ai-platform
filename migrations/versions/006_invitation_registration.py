"""Bind one-time tokens to tenant membership for invited registration.

Revision ID: 006_invitation_registration
Revises: 005_control_plane_security

The online migration refuses to proceed when legacy token rows exist because
the previous schema cannot prove their organization or membership context.
Operators must archive and remove those rows through an explicitly reviewed
procedure before retrying; this migration never deletes authentication data.
"""

from __future__ import annotations

from alembic import context, op
from sqlalchemy import CHAR, Column, Integer, inspect, text


revision = "006_invitation_registration"
down_revision = "005_control_plane_security"
branch_labels = None
depends_on = None

TABLE_NAME = "one_time_tokens"
OLD_USER_FK = "fk_one_time_tokens_user"
CONTEXT_FK = "fk_one_time_tokens_membership_context"
PURPOSE_CHECK = "ck_one_time_tokens_invitation_purpose"
IDENTITY_VERSION_CHECK = "ck_one_time_tokens_identity_version_positive"
ORGANIZATION_INDEX = "ix_one_time_tokens_organization_purpose_expires"


def upgrade() -> None:
    """Reject unbound legacy rows and require a complete invitation context."""

    if context.is_offline_mode():
        _add_binding_columns()
        _make_binding_columns_required()
        op.drop_constraint(OLD_USER_FK, TABLE_NAME, type_="foreignkey")
        _create_context_constraints()
        return

    columns = _column_map()
    _validate_no_unbound_tokens(columns)
    if "organization_id" not in columns:
        op.add_column(TABLE_NAME, Column("organization_id", CHAR(36), nullable=True))
    if "membership_id" not in columns:
        op.add_column(TABLE_NAME, Column("membership_id", CHAR(36), nullable=True))
    if "identity_version" not in columns:
        op.add_column(
            TABLE_NAME,
            Column(
                "identity_version",
                Integer,
                nullable=True,
                comment="Snapshot of users.auth_version when the token was issued.",
            ),
        )

    _make_binding_columns_required()

    if _foreign_key_exists(OLD_USER_FK):
        op.drop_constraint(OLD_USER_FK, TABLE_NAME, type_="foreignkey")
    _create_context_constraints()


def downgrade() -> None:
    """Restore the generic 005 token shape without modifying token rows."""

    if context.is_offline_mode():
        _drop_context_constraints()
        op.create_foreign_key(
            OLD_USER_FK,
            TABLE_NAME,
            "users",
            ["user_id"],
            ["id"],
            ondelete="CASCADE",
        )
        _drop_binding_columns()
        return

    if _foreign_key_exists(CONTEXT_FK):
        op.drop_constraint(CONTEXT_FK, TABLE_NAME, type_="foreignkey")
    if _check_exists(PURPOSE_CHECK):
        op.drop_constraint(PURPOSE_CHECK, TABLE_NAME, type_="check")
    if _check_exists(IDENTITY_VERSION_CHECK):
        op.drop_constraint(IDENTITY_VERSION_CHECK, TABLE_NAME, type_="check")
    if _index_exists(ORGANIZATION_INDEX):
        op.drop_index(ORGANIZATION_INDEX, table_name=TABLE_NAME)
    if not _foreign_key_exists(OLD_USER_FK):
        op.create_foreign_key(
            OLD_USER_FK,
            TABLE_NAME,
            "users",
            ["user_id"],
            ["id"],
            ondelete="CASCADE",
        )
    columns = _column_map()
    for column_name in ("identity_version", "membership_id", "organization_id"):
        if column_name in columns:
            op.drop_column(TABLE_NAME, column_name)


def _add_binding_columns() -> None:
    op.add_column(TABLE_NAME, Column("organization_id", CHAR(36), nullable=True))
    op.add_column(TABLE_NAME, Column("membership_id", CHAR(36), nullable=True))
    op.add_column(
        TABLE_NAME,
        Column(
            "identity_version",
            Integer,
            nullable=True,
            comment="Snapshot of users.auth_version when the token was issued.",
        ),
    )


def _validate_no_unbound_tokens(columns: dict[str, dict[str, object]]) -> None:
    """Fail before DDL rather than guessing or deleting an unbound token.

    A previous MySQL DDL failure may have added only part of the binding
    columns. In that case every row is still treated as legacy. When all three
    columns exist, fully bound rows may remain while any NULL context aborts
    the retry with the same stable preflight error.
    """

    binding_columns = ("organization_id", "membership_id", "identity_version")
    if any(column_name not in columns for column_name in binding_columns):
        count_statement = text("SELECT COUNT(*) FROM one_time_tokens")
    else:
        count_statement = text(
            "SELECT COUNT(*) FROM one_time_tokens "
            "WHERE organization_id IS NULL "
            "OR membership_id IS NULL "
            "OR identity_version IS NULL"
        )
    unbound_count = op.get_bind().execute(count_statement).scalar_one()
    if unbound_count:
        raise RuntimeError(
            "006 preflight failed: legacy one_time_tokens must be explicitly "
            "archived and removed before tenant binding can be enabled"
        )


def _make_binding_columns_required() -> None:
    op.alter_column(
        TABLE_NAME,
        "organization_id",
        existing_type=CHAR(36),
        existing_nullable=True,
        nullable=False,
    )
    op.alter_column(
        TABLE_NAME,
        "membership_id",
        existing_type=CHAR(36),
        existing_nullable=True,
        nullable=False,
    )
    op.alter_column(
        TABLE_NAME,
        "identity_version",
        existing_type=Integer(),
        existing_nullable=True,
        nullable=False,
        existing_comment="Snapshot of users.auth_version when the token was issued.",
    )


def _create_context_constraints() -> None:
    if context.is_offline_mode() or not _foreign_key_exists(CONTEXT_FK):
        op.create_foreign_key(
            CONTEXT_FK,
            TABLE_NAME,
            "organization_memberships",
            ["membership_id", "user_id", "organization_id"],
            ["id", "user_id", "organization_id"],
            ondelete="CASCADE",
        )
    if context.is_offline_mode() or not _check_exists(PURPOSE_CHECK):
        op.create_check_constraint(
            PURPOSE_CHECK,
            TABLE_NAME,
            "purpose = 'user_invitation'",
        )
    if context.is_offline_mode() or not _check_exists(IDENTITY_VERSION_CHECK):
        op.create_check_constraint(
            IDENTITY_VERSION_CHECK,
            TABLE_NAME,
            "identity_version >= 1",
        )
    if context.is_offline_mode() or not _index_exists(ORGANIZATION_INDEX):
        op.create_index(
            ORGANIZATION_INDEX,
            TABLE_NAME,
            ["organization_id", "purpose", "expires_at"],
        )


def _drop_context_constraints() -> None:
    op.drop_constraint(CONTEXT_FK, TABLE_NAME, type_="foreignkey")
    op.drop_constraint(PURPOSE_CHECK, TABLE_NAME, type_="check")
    op.drop_constraint(IDENTITY_VERSION_CHECK, TABLE_NAME, type_="check")
    op.drop_index(ORGANIZATION_INDEX, table_name=TABLE_NAME)


def _drop_binding_columns() -> None:
    op.drop_column(TABLE_NAME, "identity_version")
    op.drop_column(TABLE_NAME, "membership_id")
    op.drop_column(TABLE_NAME, "organization_id")


def _inspector():
    return inspect(op.get_bind())


def _column_map() -> dict[str, dict[str, object]]:
    return {column["name"]: column for column in _inspector().get_columns(TABLE_NAME)}


def _foreign_key_exists(name: str) -> bool:
    return any(
        foreign_key.get("name") == name
        for foreign_key in _inspector().get_foreign_keys(TABLE_NAME)
    )


def _check_exists(name: str) -> bool:
    return any(
        constraint.get("name") == name
        for constraint in _inspector().get_check_constraints(TABLE_NAME)
    )


def _index_exists(name: str) -> bool:
    return any(index.get("name") == name for index in _inspector().get_indexes(TABLE_NAME))
