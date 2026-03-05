"""create core pilot tables

Revision ID: 20260304_0001
Revises:
Create Date: 2026-03-04 10:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "20260304_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "documents",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("organisation_id", sa.Integer(), nullable=True),
        sa.Column("subsidiary_id", sa.String(length=64), nullable=True),
        sa.Column("organisation_size", sa.String(length=1), nullable=True),
        sa.Column("submission_period", sa.String(length=16), nullable=True),
        sa.Column("original_filename", sa.String(length=255), nullable=False),
        sa.Column("mime_type", sa.String(length=128), nullable=False),
        sa.Column("storage_path", sa.String(length=512), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="uploaded"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )

    op.create_table(
        "pages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "document_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("page_number", sa.Integer(), nullable=False),
        sa.Column("image_path", sa.String(length=512), nullable=True),
        sa.Column("ocr_text", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )

    op.create_table(
        "entities",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "page_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("pages.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("label", sa.String(length=64), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Numeric(5, 4), nullable=True),
        sa.Column("start_offset", sa.Integer(), nullable=True),
        sa.Column("end_offset", sa.Integer(), nullable=True),
        sa.Column(
            "entity_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )

    op.create_table(
        "classifications",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "document_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("row_index", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("taxonomy_category", sa.String(length=128), nullable=True),
        sa.Column("taxonomy_code", sa.String(length=64), nullable=True),
        sa.Column("packaging_activity", sa.String(length=32), nullable=True),
        sa.Column("packaging_type", sa.String(length=32), nullable=True),
        sa.Column("packaging_class", sa.String(length=32), nullable=True),
        sa.Column("packaging_material", sa.String(length=128), nullable=True),
        sa.Column("packaging_material_subtype", sa.String(length=128), nullable=True),
        sa.Column("from_country", sa.String(length=64), nullable=True),
        sa.Column("to_country", sa.String(length=64), nullable=True),
        sa.Column("packaging_material_weight", sa.Numeric(12, 2), nullable=True),
        sa.Column("packaging_material_units", sa.Integer(), nullable=True),
        sa.Column("transitional_packaging_units", sa.Integer(), nullable=True),
        sa.Column("ram_rag_rating", sa.String(length=16), nullable=True),
        sa.Column("confidence", sa.Numeric(5, 4), nullable=True),
        sa.Column("source", sa.String(length=32), nullable=False, server_default="mocked"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )

    op.create_table(
        "review_tasks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "document_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "classification_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("classifications.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("task_type", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("assigned_to", sa.String(length=128), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )

    op.create_table(
        "audit_events",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True, nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("entity_type", sa.String(length=64), nullable=False),
        sa.Column("entity_id", sa.String(length=128), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )

    op.create_table(
        "reports",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "document_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("submission_period", sa.String(length=16), nullable=True),
        sa.Column("output_path", sa.String(length=512), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column("row_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )

    op.create_table(
        "taxonomy_codes",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True, nullable=False),
        sa.Column("category", sa.String(length=128), nullable=False),
        sa.Column("code", sa.String(length=128), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("source_sheet", sa.String(length=255), nullable=False),
        sa.Column("source_row_number", sa.Integer(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("category", "code", name="uq_taxonomy_codes_category_code"),
    )

    op.create_index("ix_pages_document_id_page_number", "pages", ["document_id", "page_number"])
    op.create_index(
        "ix_classifications_document_id_row_index", "classifications", ["document_id", "row_index"]
    )
    op.create_index("ix_review_tasks_document_id_status", "review_tasks", ["document_id", "status"])
    op.create_index("ix_audit_events_entity", "audit_events", ["entity_type", "entity_id"])
    op.create_index("ix_reports_document_id", "reports", ["document_id"])
    op.create_index("ix_taxonomy_codes_category_code", "taxonomy_codes", ["category", "code"])


def downgrade() -> None:
    op.drop_index("ix_taxonomy_codes_category_code", table_name="taxonomy_codes")
    op.drop_index("ix_reports_document_id", table_name="reports")
    op.drop_index("ix_audit_events_entity", table_name="audit_events")
    op.drop_index("ix_review_tasks_document_id_status", table_name="review_tasks")
    op.drop_index("ix_classifications_document_id_row_index", table_name="classifications")
    op.drop_index("ix_pages_document_id_page_number", table_name="pages")

    op.drop_table("taxonomy_codes")
    op.drop_table("reports")
    op.drop_table("audit_events")
    op.drop_table("review_tasks")
    op.drop_table("classifications")
    op.drop_table("entities")
    op.drop_table("pages")
    op.drop_table("documents")
