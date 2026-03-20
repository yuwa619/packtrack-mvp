"""add training samples table

Revision ID: 20260305_0007
Revises: 20260304_0006
Create Date: 2026-03-05 10:45:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "20260305_0007"
down_revision = "20260304_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "training_samples",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "document_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("page_number", sa.Integer(), nullable=False),
        sa.Column("raw_ocr_text_span", sa.Text(), nullable=False),
        sa.Column("corrected_value", sa.Text(), nullable=False),
        sa.Column("field_name", sa.String(length=64), nullable=False),
        sa.Column("reviewer", sa.String(length=128), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index(
        "ix_training_samples_document_field",
        "training_samples",
        ["document_id", "field_name"],
    )


def downgrade() -> None:
    op.drop_index("ix_training_samples_document_field", table_name="training_samples")
    op.drop_table("training_samples")
