"""MVP-006: catalog tables and PostgreSQL constraints/search indexes."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002_mvp006_catalog"
down_revision = "0002_mvp004_staff"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "equipment_models",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.String(100), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), server_default="draft", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_equipment_models"),
        sa.UniqueConstraint("code", name="uq_equipment_models_code"),
        sa.CheckConstraint(
            "status IN ('draft', 'published', 'archived')",
            name=op.f("ck_equipment_models_status"),
        ),
        schema="app",
    )
    op.create_table(
        "catalog_items",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("sku", sa.String(200), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column(
            "attributes",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("unit", sa.String(200), nullable=False),
        sa.Column("price_mode", sa.String(16), nullable=False),
        sa.Column("price", sa.Numeric(12, 2, asdecimal=True), nullable=True),
        sa.Column("currency", sa.String(3), server_default="RUB", nullable=False),
        sa.Column("tax_note", sa.String(200), nullable=False),
        sa.Column(
            "requires_shipping", sa.Boolean(), server_default=sa.false(), nullable=False
        ),
        sa.Column("image_key", sa.Text(), nullable=True),
        sa.Column("status", sa.String(16), server_default="draft", nullable=False),
        sa.Column(
            "revision", sa.Integer(), server_default=sa.text("1"), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_catalog_items"),
        sa.UniqueConstraint("sku", name="uq_catalog_items_sku"),
        sa.CheckConstraint(
            "kind IN ('product', 'service')", name=op.f("ck_catalog_items_kind")
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'published', 'archived')",
            name=op.f("ck_catalog_items_status"),
        ),
        sa.CheckConstraint(
            "price_mode IN ('fixed', 'on_request')",
            name=op.f("ck_catalog_items_price_mode"),
        ),
        sa.CheckConstraint(
            "(price_mode = 'fixed' AND price IS NOT NULL AND price >= 0 "
            "AND price <> 'NaN'::numeric) "
            "OR (price_mode = 'on_request' AND price IS NULL)",
            name=op.f("ck_catalog_items_price"),
        ),
        sa.CheckConstraint(
            "kind <> 'service' OR NOT requires_shipping",
            name=op.f("ck_catalog_items_shipping"),
        ),
        sa.CheckConstraint("revision > 0", name=op.f("ck_catalog_items_revision")),
        sa.CheckConstraint("currency = 'RUB'", name=op.f("ck_catalog_items_currency")),
        sa.CheckConstraint(
            "jsonb_typeof(attributes) = 'object'",
            name=op.f("ck_catalog_items_attributes_object"),
        ),
        schema="app",
    )
    for column in ("name", "sku"):
        op.create_index(
            f"ix_catalog_items_{column}_trgm",
            "catalog_items",
            [column],
            schema="app",
            postgresql_using="gin",
            postgresql_ops={column: "public.gin_trgm_ops"},
        )
    op.create_index(
        "ix_catalog_items_published_kind",
        "catalog_items",
        ["kind"],
        schema="app",
        postgresql_where=sa.text("status = 'published'"),
    )


def downgrade() -> None:
    # Explicit operator rollback only; normal upgrade never drops existing data.
    op.drop_table("catalog_items", schema="app")
    op.drop_table("equipment_models", schema="app")
