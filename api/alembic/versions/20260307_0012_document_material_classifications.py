"""add document material classifications

Revision ID: 20260307_0012
Revises: 20260305_0011
Create Date: 2026-03-07 10:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "20260307_0012"
down_revision = "20260305_0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "document_material_classifications",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "document_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("taxonomy_category", sa.String(length=128), nullable=True),
        sa.Column("taxonomy_code", sa.String(length=128), nullable=True),
        sa.Column("packaging_material", sa.String(length=128), nullable=False),
        sa.Column("packaging_material_subtype", sa.String(length=128), nullable=True),
        sa.Column("packaging_material_weight", sa.Numeric(precision=12, scale=3), nullable=True),
        sa.Column("packaging_material_units", sa.String(length=16), nullable=True),
        sa.Column("confidence", sa.Numeric(precision=5, scale=4), nullable=True),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_document_material_classifications_document_id",
        "document_material_classifications",
        ["document_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_document_material_classifications_document_id",
        table_name="document_material_classifications",
    )
    op.drop_table("document_material_classifications")
