"""Merge gateway and CarbonTeq run-lifecycle migration heads.

Revision ID: 8d9e0f1a2b3c
Revises: 7c8d9e0f1a2b, dd83c131e78f
Create Date: 2026-08-31 18:00:00+00:00
"""

revision = "8d9e0f1a2b3c"
down_revision = ("7c8d9e0f1a2b", "dd83c131e78f")
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Join the independently published migration branches."""


def downgrade() -> None:
    """Split back to the two parent migration heads."""
