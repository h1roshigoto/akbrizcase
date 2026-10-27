"""MVP-024 reviewed applicability candidates and separate public permission."""

import sqlalchemy as sa
from alembic import op

revision = "0005_mvp024_applicability"
down_revision = "0004_mvp019_documents"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "catalog_item_models",
        sa.Column("item_id", sa.Uuid(), nullable=False),
        sa.Column("model_id", sa.Uuid(), nullable=False),
        sa.Column("evidence_section_id", sa.Uuid(), nullable=False),
        sa.Column("expert_name", sa.String(200), nullable=True),
        sa.Column("review_reference", sa.String(200), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "public_allowed", sa.Boolean(), server_default=sa.false(), nullable=False
        ),
        sa.Column("published_by", sa.Uuid(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint(
            "item_id", "model_id", name=op.f("pk_catalog_item_models")
        ),
        sa.CheckConstraint(
            "expert_name IS NULL OR (length(expert_name) BETWEEN 3 AND 200 "
            "AND expert_name = btrim(expert_name) "
            "AND expert_name !~ '[[:cntrl:]]')",
            name=op.f("ck_catalog_item_models_expert_name"),
        ),
        sa.CheckConstraint(
            "review_reference IS NULL OR "
            "review_reference ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{2,199}$'",
            name=op.f("ck_catalog_item_models_review_reference"),
        ),
        sa.CheckConstraint(
            "approved_at IS NULL OR isfinite(approved_at)",
            name=op.f("ck_catalog_item_models_approved_at"),
        ),
        sa.CheckConstraint(
            "(NOT public_allowed AND published_by IS NULL AND published_at IS NULL) "
            "OR (public_allowed AND expert_name IS NOT NULL "
            "AND review_reference IS NOT NULL AND approved_at IS NOT NULL "
            "AND published_by IS NOT NULL AND published_at IS NOT NULL "
            "AND isfinite(published_at) AND approved_at <= published_at)",
            name=op.f("ck_catalog_item_models_publication"),
        ),
        *[
            sa.ForeignKeyConstraint(
                [column],
                [f"app.{table}.id"],
                ondelete="RESTRICT",
                name=op.f(f"fk_catalog_item_models_{column}_{table}"),
            )
            for column, table in (
                ("item_id", "catalog_items"),
                ("model_id", "equipment_models"),
                ("evidence_section_id", "document_sections"),
                ("published_by", "staff_users"),
            )
        ],
        schema="app",
    )
    op.create_index(
        "ix_catalog_item_models_model_item",
        "catalog_item_models",
        ["model_id", "item_id"],
        schema="app",
    )
    op.create_index(
        "ix_catalog_item_models_evidence_section_id",
        "catalog_item_models",
        ["evidence_section_id"],
        schema="app",
    )


def downgrade() -> None:
    raise RuntimeError(
        "Applicability history must be preserved; downgrade is unsupported"
    )
