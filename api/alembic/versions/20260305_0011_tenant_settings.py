"""add tenant settings

Revision ID: 20260305_0011
Revises: 20260305_0010
Create Date: 2026-03-05 15:25:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "20260305_0011"
down_revision = "20260305_0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tenant_settings",
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("ner_enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id"),
    )


def downgrade() -> None:
    op.drop_table("tenant_settings")
