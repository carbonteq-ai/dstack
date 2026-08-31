"""Add logical run ownership to managed volumes.

Revision ID: 6b7c8d9e0f1a
Revises: 5a6b7c8d9e0f
Create Date: 2026-08-30 15:00:00+00:00
"""

import sqlalchemy as sa
import sqlalchemy_utils
from alembic import op

revision = "6b7c8d9e0f1a"
down_revision = "5a6b7c8d9e0f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("volumes", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "run_id",
                sqlalchemy_utils.types.uuid.UUIDType(binary=False),
                nullable=True,
            )
        )
        batch_op.create_foreign_key(
            batch_op.f("fk_volumes_run_id_runs"),
            "runs",
            ["run_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch_op.create_index(batch_op.f("ix_volumes_run_id"), ["run_id"], unique=True)


def downgrade() -> None:
    with op.batch_alter_table("volumes", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_volumes_run_id"))
        batch_op.drop_constraint(batch_op.f("fk_volumes_run_id_runs"), type_="foreignkey")
        batch_op.drop_column("run_id")
