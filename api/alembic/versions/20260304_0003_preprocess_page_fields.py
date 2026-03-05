"""add preprocess metadata fields to pages

Revision ID: 20260304_0003
Revises: 20260304_0002
Create Date: 2026-03-04 11:20:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "20260304_0003"
down_revision = "20260304_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("pages", sa.Column("page_width", sa.Integer(), nullable=True))
    op.add_column("pages", sa.Column("page_height", sa.Integer(), nullable=True))
    op.add_column("pages", sa.Column("raw_image_path", sa.String(length=512), nullable=True))
    op.add_column("pages", sa.Column("normalised_image_path", sa.String(length=512), nullable=True))
    op.add_column("pages", sa.Column("processing_time_ms", sa.Numeric(12, 3), nullable=True))


def downgrade() -> None:
    op.drop_column("pages", "processing_time_ms")
    op.drop_column("pages", "normalised_image_path")
    op.drop_column("pages", "raw_image_path")
    op.drop_column("pages", "page_height")
    op.drop_column("pages", "page_width")
