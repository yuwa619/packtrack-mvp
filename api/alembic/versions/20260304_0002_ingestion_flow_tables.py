"""add ingestion upload sessions and jobs

Revision ID: 20260304_0002
Revises: 20260304_0001
Create Date: 2026-03-04 11:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "20260304_0002"
down_revision = "20260304_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("documents", sa.Column("file_size_bytes", sa.BigInteger(), nullable=True))
    op.add_column("documents", sa.Column("checksum_sha256", sa.String(length=64), nullable=True))
    op.add_column("documents", sa.Column("uploaded_by", sa.String(length=128), nullable=True))

    op.create_table(
        "upload_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.String(length=128), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("mime_type", sa.String(length=128), nullable=False),
        sa.Column("expected_size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("bucket_name", sa.String(length=128), nullable=False),
        sa.Column("object_key", sa.String(length=512), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="PRESIGNED"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("finalised_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_upload_sessions_tenant_status", "upload_sessions", ["tenant_id", "status"])

    op.create_table(
        "jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "document_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("organisation_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="QUEUED"),
        sa.Column("current_stage", sa.String(length=32), nullable=False, server_default="QUEUED"),
        sa.Column("queue_name", sa.String(length=128), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_jobs_org_status", "jobs", ["organisation_id", "status"])


def downgrade() -> None:
    op.drop_index("ix_jobs_org_status", table_name="jobs")
    op.drop_table("jobs")

    op.drop_index("ix_upload_sessions_tenant_status", table_name="upload_sessions")
    op.drop_table("upload_sessions")

    op.drop_column("documents", "uploaded_by")
    op.drop_column("documents", "checksum_sha256")
    op.drop_column("documents", "file_size_bytes")
