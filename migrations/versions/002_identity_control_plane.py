"""Create identity, authorization, audit, job, and dataset control tables.

Revision ID: 002_identity_control_plane
Revises: 001_existing_analytics_baseline
"""

from __future__ import annotations

from pathlib import Path

from alembic import op

revision = "002_identity_control_plane"
down_revision = "001_existing_analytics_baseline"
branch_labels = None
depends_on = None

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SQL_MIGRATIONS = PROJECT_ROOT / "infra" / "mysql" / "migrations"


def upgrade() -> None:
    """Apply the idempotent, reviewed MySQL control-plane DDL."""

    _execute_sql_file("002_identity_control_plane.up.sql")


def downgrade() -> None:
    """Drop only the control-plane tables in reverse dependency order."""

    _execute_sql_file("002_identity_control_plane.down.sql")


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
