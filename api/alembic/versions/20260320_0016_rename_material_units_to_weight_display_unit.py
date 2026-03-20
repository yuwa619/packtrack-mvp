"""Rename packaging_material_units to weight_display_unit on document_material_classifications.

The column was incorrectly named after the DEFRA CSV column 'packaging_material_units'
(which holds a numeric item count), but it actually stored a weight-unit string such as
'kg' or 'tonnes'.  Renaming clarifies the semantic difference and prevents the value
from being exported into the wrong CSV column.

Revision ID: 0016
Revises: 0015
"""

from alembic import op

revision = "20260320_0016"
down_revision = "20260312_0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "document_material_classifications",
        "packaging_material_units",
        new_column_name="weight_display_unit",
    )


def downgrade() -> None:
    op.alter_column(
        "document_material_classifications",
        "weight_display_unit",
        new_column_name="packaging_material_units",
    )
