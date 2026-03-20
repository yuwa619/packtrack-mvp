"""make training samples ner-ready

Revision ID: 20260305_0009
Revises: 20260305_0008
Create Date: 2026-03-05 11:45:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "20260305_0009"
down_revision = "20260305_0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "training_samples",
        "raw_ocr_text_span",
        new_column_name="ocr_text",
        existing_type=sa.Text(),
        existing_nullable=False,
    )
    op.add_column("training_samples", sa.Column("span_start", sa.Integer(), nullable=True))
    op.add_column("training_samples", sa.Column("span_end", sa.Integer(), nullable=True))
    op.add_column(
        "training_samples",
        sa.Column(
            "source",
            sa.String(length=64),
            nullable=False,
            server_default="field_correction",
        ),
    )


def downgrade() -> None:
    op.drop_column("training_samples", "source")
    op.drop_column("training_samples", "span_end")
    op.drop_column("training_samples", "span_start")
    op.alter_column(
        "training_samples",
        "ocr_text",
        new_column_name="raw_ocr_text_span",
        existing_type=sa.Text(),
        existing_nullable=False,
    )
