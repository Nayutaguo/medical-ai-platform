"""Record the existing analytics schema as a migration baseline.

Revision ID: 001_existing_analytics_baseline
Revises: None
"""

from __future__ import annotations

revision = "001_existing_analytics_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Leave the pre-existing inpatient table untouched."""


def downgrade() -> None:
    """Never drop the independently managed medical-data fact table."""
