"""add document metadata fields

Revision ID: 20260312_0015
Revises: 20260312_0014
Create Date: 2026-03-12 12:30:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "20260312_0015"
down_revision = "20260312_0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("documents", sa.Column("document_type", sa.String(length=64), nullable=True))
    op.add_column("documents", sa.Column("document_date", sa.String(length=10), nullable=True))
    op.add_column(
        "documents",
        sa.Column("inferred_country_code", sa.String(length=8), nullable=True),
    )
    op.add_column(
        "documents",
        sa.Column("country_inference_source", sa.String(length=32), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("documents", "country_inference_source")
    op.drop_column("documents", "inferred_country_code")
    op.drop_column("documents", "document_date")
    op.drop_column("documents", "document_type")
