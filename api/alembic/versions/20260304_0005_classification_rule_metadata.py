"""add classification candidates and taxonomy metadata

Revision ID: 20260304_0005
Revises: 20260304_0004
Create Date: 2026-03-04 12:30:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "20260304_0005"
down_revision = "20260304_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "classifications", sa.Column("taxonomy_version", sa.String(length=128), nullable=True)
    )
    op.add_column(
        "classifications",
        sa.Column(
            "candidate_codes",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column("classifications", sa.Column("rule_id", sa.String(length=128), nullable=True))
    op.add_column("classifications", sa.Column("rule_reason", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("classifications", "rule_reason")
    op.drop_column("classifications", "rule_id")
    op.drop_column("classifications", "candidate_codes")
    op.drop_column("classifications", "taxonomy_version")
