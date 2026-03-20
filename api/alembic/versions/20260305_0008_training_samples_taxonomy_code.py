"""add taxonomy_code to training samples

Revision ID: 20260305_0008
Revises: 20260305_0007
Create Date: 2026-03-05 11:10:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "20260305_0008"
down_revision = "20260305_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "training_samples",
        sa.Column("taxonomy_code", sa.String(length=128), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("training_samples", "taxonomy_code")
