"""Add persisted job image readiness snapshot.

Revision ID: 5a6b7c8d9e0f
Revises: 4d3cbb932bb2
Create Date: 2026-08-30 12:00:00+00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "5a6b7c8d9e0f"
down_revision = "4d3cbb932bb2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("image_readiness", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("jobs", "image_readiness")
