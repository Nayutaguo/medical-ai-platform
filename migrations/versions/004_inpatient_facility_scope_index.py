"""Add the mandatory analytics index used by facility-scoped queries.

Revision ID: 004_facility_scope_index
Revises: 003_facility_scopes
"""

from __future__ import annotations

from alembic import context, op
from sqlalchemy import inspect

revision = "004_facility_scope_index"
down_revision = "003_facility_scopes"
branch_labels = None
depends_on = None

INDEX_NAME = "idx_inpatient_facility_year"
TABLE_NAME = "inpatient"


def upgrade() -> None:
    """Create a prefix/composite index for the trusted facility predicate."""

    if context.is_offline_mode() or not _index_exists(INDEX_NAME):
        op.execute(
            "CREATE INDEX `idx_inpatient_facility_year` "
            "ON `inpatient` (`PermanentFacilityId`(128), `DischargeYear`)"
        )


def downgrade() -> None:
    """Remove only the facility-scope query index."""

    if context.is_offline_mode() or _index_exists(INDEX_NAME):
        op.execute("DROP INDEX `idx_inpatient_facility_year` ON `inpatient`")


def _index_exists(index_name: str) -> bool:
    """Check the live schema so deployments remain safe after manual pre-creation."""

    return any(
        index.get("name") == index_name
        for index in inspect(op.get_bind()).get_indexes(TABLE_NAME)
    )
