"""add report warnings and material key

Revision ID: 20260307_0013
Revises: 20260307_0012
Create Date: 2026-03-07 12:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "20260307_0013"
down_revision = "20260307_0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "document_material_classifications",
        sa.Column("material_key", sa.String(length=64), nullable=True),
    )
    op.execute(
        """
        UPDATE document_material_classifications
        SET material_key = CASE
            WHEN packaging_material_subtype IS NOT NULL AND packaging_material_subtype <> ''
                THEN packaging_material || ' ' || packaging_material_subtype
            ELSE packaging_material
        END
        """
    )
    op.alter_column(
        "document_material_classifications",
        "material_key",
        existing_type=sa.String(length=64),
        nullable=False,
    )

    op.add_column(
        "reports",
        sa.Column(
            "validation_warnings",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.alter_column("reports", "validation_warnings", server_default=None)


def downgrade() -> None:
    op.drop_column("reports", "validation_warnings")
    op.drop_column("document_material_classifications", "material_key")
