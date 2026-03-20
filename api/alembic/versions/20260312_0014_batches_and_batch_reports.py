"""add batches and batch-backed reports

Revision ID: 20260312_0014
Revises: 20260307_0013
Create Date: 2026-03-12 11:30:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "20260312_0014"
down_revision = "20260307_0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "batches",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.String(length=128), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "batch_documents",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("batch_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["batch_id"], ["batches.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("batch_id", "document_id", name="uq_batch_documents_batch_document"),
    )
    op.create_index("ix_batch_documents_batch_id", "batch_documents", ["batch_id"])
    op.create_index("ix_batch_documents_document_id", "batch_documents", ["document_id"])

    op.add_column(
        "upload_sessions",
        sa.Column("batch_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_upload_sessions_batch_id_batches",
        "upload_sessions",
        "batches",
        ["batch_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_upload_sessions_batch_id", "upload_sessions", ["batch_id"])

    op.add_column(
        "reports",
        sa.Column("batch_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_reports_batch_id_batches",
        "reports",
        "batches",
        ["batch_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index("ix_reports_batch_id", "reports", ["batch_id"])

    op.alter_column(
        "reports",
        "document_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "reports",
        "document_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=False,
    )

    op.drop_index("ix_reports_batch_id", table_name="reports")
    op.drop_constraint("fk_reports_batch_id_batches", "reports", type_="foreignkey")
    op.drop_column("reports", "batch_id")

    op.drop_index("ix_upload_sessions_batch_id", table_name="upload_sessions")
    op.drop_constraint(
        "fk_upload_sessions_batch_id_batches", "upload_sessions", type_="foreignkey"
    )
    op.drop_column("upload_sessions", "batch_id")

    op.drop_index("ix_batch_documents_document_id", table_name="batch_documents")
    op.drop_index("ix_batch_documents_batch_id", table_name="batch_documents")
    op.drop_table("batch_documents")
    op.drop_table("batches")
