"""add ner model provenance to documents and jobs

Revision ID: 20260305_0010
Revises: 20260305_0009
Create Date: 2026-03-05 14:25:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "20260305_0010"
down_revision = "20260305_0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("documents", sa.Column("ner_model_path", sa.String(length=512), nullable=True))
    op.add_column(
        "documents",
        sa.Column("ner_model_trained_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("documents", sa.Column("ner_model_f1", sa.Numeric(5, 4), nullable=True))

    op.add_column("jobs", sa.Column("ner_model_path", sa.String(length=512), nullable=True))
    op.add_column(
        "jobs",
        sa.Column("ner_model_trained_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("jobs", sa.Column("ner_model_f1", sa.Numeric(5, 4), nullable=True))


def downgrade() -> None:
    op.drop_column("jobs", "ner_model_f1")
    op.drop_column("jobs", "ner_model_trained_at")
    op.drop_column("jobs", "ner_model_path")

    op.drop_column("documents", "ner_model_f1")
    op.drop_column("documents", "ner_model_trained_at")
    op.drop_column("documents", "ner_model_path")
