"""add idempotency records table

Revision ID: 20260304_0006
Revises: 20260304_0005
Create Date: 2026-03-04 15:20:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "20260304_0006"
down_revision = "20260304_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "idempotency_records",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("scope", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="IN_PROGRESS"),
        sa.Column("response_code", sa.Integer(), nullable=True),
        sa.Column(
            "response_payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "scope",
            "idempotency_key",
            name="uq_idempotency_records_tenant_scope_key",
        ),
    )
    op.create_index(
        "ix_idempotency_records_tenant_scope_status",
        "idempotency_records",
        ["tenant_id", "scope", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_idempotency_records_tenant_scope_status", table_name="idempotency_records")
    op.drop_table("idempotency_records")
