"""MVP-019 document editions, stable sections, Russian FTS and manual FK."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0004_mvp019_documents"
down_revision = "0003_mvp008_orders"
branch_labels = None
depends_on = None


def timestamps() -> list[sa.Column[sa.DateTime]]:
    return [
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    ]


def upgrade() -> None:
    op.create_table(
        "documents",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.String(100), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("visibility", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), server_default="active", nullable=False),
        *timestamps(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_documents")),
        sa.UniqueConstraint("code", name=op.f("uq_documents_code")),
        sa.CheckConstraint(
            "kind IN ('manual', 'regulation', 'article', 'other')",
            name=op.f("ck_documents_kind"),
        ),
        sa.CheckConstraint(
            "visibility IN ('public', 'internal')", name=op.f("ck_documents_visibility")
        ),
        sa.CheckConstraint(
            "status IN ('active', 'revoked')", name=op.f("ck_documents_status")
        ),
        sa.CheckConstraint("btrim(code) <> ''", name=op.f("ck_documents_code")),
        sa.CheckConstraint("btrim(title) <> ''", name=op.f("ck_documents_title")),
        schema="app",
    )
    op.create_table(
        "document_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("file_key", sa.String(200), nullable=False),
        sa.Column("mime", sa.String(100), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("checksum_sha256", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), server_default="draft", nullable=False),
        sa.Column("reviewed_by", sa.Uuid(), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        *timestamps(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_document_versions")),
        sa.UniqueConstraint(
            "document_id", "version", name=op.f("uq_document_versions_document_id")
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["app.documents.id"],
            ondelete="RESTRICT",
            name=op.f("fk_document_versions_document_id_documents"),
        ),
        sa.ForeignKeyConstraint(
            ["reviewed_by"],
            ["app.staff_users.id"],
            ondelete="RESTRICT",
            name=op.f("fk_document_versions_reviewed_by_staff_users"),
        ),
        sa.CheckConstraint("version > 0", name=op.f("ck_document_versions_version")),
        sa.CheckConstraint(
            "status IN ('draft', 'published', 'retired', 'revoked')",
            name=op.f("ck_document_versions_status"),
        ),
        sa.CheckConstraint(
            "size_bytes BETWEEN 1 AND 26214400",
            name=op.f("ck_document_versions_size_bytes"),
        ),
        sa.CheckConstraint(
            "checksum_sha256 ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_document_versions_checksum_sha256"),
        ),
        sa.CheckConstraint(
            "file_key ~ '^[A-Za-z0-9][A-Za-z0-9_-]{0,199}$'",
            name=op.f("ck_document_versions_file_key"),
        ),
        sa.CheckConstraint("btrim(mime) <> ''", name=op.f("ck_document_versions_mime")),
        schema="app",
    )
    op.create_index(
        "ix_document_versions_published",
        "document_versions",
        ["document_id"],
        unique=True,
        schema="app",
        postgresql_where=sa.text("status = 'published'"),
    )
    op.create_table(
        "document_sections",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("document_version_id", sa.Uuid(), nullable=False),
        sa.Column("section_key", sa.String(200), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("page_from", sa.Integer(), nullable=True),
        sa.Column("page_to", sa.Integer(), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column(
            "assistant_allowed",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "search_vector",
            postgresql.TSVECTOR(),
            sa.Computed(
                "to_tsvector('russian'::regconfig, title || ' ' || text)",
                persisted=True,
            ),
            nullable=False,
        ),
        *timestamps(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_document_sections")),
        sa.UniqueConstraint(
            "document_version_id",
            "section_key",
            name=op.f("uq_document_sections_document_version_id"),
        ),
        sa.ForeignKeyConstraint(
            ["document_version_id"],
            ["app.document_versions.id"],
            ondelete="RESTRICT",
            name=op.f("fk_document_sections_document_version_id_document_versions"),
        ),
        sa.CheckConstraint(
            "btrim(section_key) <> ''", name=op.f("ck_document_sections_section_key")
        ),
        sa.CheckConstraint(
            "(page_from IS NULL AND page_to IS NULL) OR "
            "(page_from IS NOT NULL AND page_to IS NOT NULL "
            "AND page_from >= 1 AND page_to >= page_from)",
            name=op.f("ck_document_sections_pages"),
        ),
        schema="app",
    )
    op.create_index(
        "ix_document_sections_document_version_id",
        "document_sections",
        ["document_version_id"],
        schema="app",
    )
    op.create_index(
        "ix_document_sections_search_vector",
        "document_sections",
        ["search_vector"],
        schema="app",
        postgresql_using="gin",
    )
    op.add_column(
        "equipment_models",
        sa.Column("manual_document_id", sa.Uuid(), nullable=True),
        schema="app",
    )
    op.create_foreign_key(
        "fk_equipment_models_manual_document_id_documents",
        "equipment_models",
        "documents",
        ["manual_document_id"],
        ["id"],
        source_schema="app",
        referent_schema="app",
        ondelete="RESTRICT",
    )
    # Invoker rights, no bypass flags, fixed search_path. Default privileges from
    # provisioning grant runtime CRUD on new tables, never schema ownership/DDL.
    op.execute("""
        CREATE FUNCTION app.protect_document_version() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                IF OLD.status <> 'draft' THEN
                    RAISE EXCEPTION 'Non-draft editions are permanent'
                        USING ERRCODE = '23514';
                END IF;
                RETURN OLD;
            END IF;
            IF ROW(NEW.id, NEW.document_id, NEW.version, NEW.created_at)
                IS DISTINCT FROM
                ROW(OLD.id, OLD.document_id, OLD.version, OLD.created_at)
            THEN
                RAISE EXCEPTION 'Edition identity is immutable' USING ERRCODE = '23514';
            END IF;
            IF OLD.status <> 'draft' AND (
                NEW.status = 'draft' OR
                (OLD.status = 'revoked' AND NEW.status <> 'revoked') OR
                ROW(NEW.file_key, NEW.mime, NEW.size_bytes, NEW.checksum_sha256,
                    NEW.reviewed_by, NEW.approved_at, NEW.published_at)
                IS DISTINCT FROM
                ROW(OLD.file_key, OLD.mime, OLD.size_bytes, OLD.checksum_sha256,
                    OLD.reviewed_by, OLD.approved_at, OLD.published_at)
            ) THEN
                RAISE EXCEPTION 'Non-draft edition content is immutable'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER document_version_immutable BEFORE UPDATE OR DELETE
        ON app.document_versions FOR EACH ROW
        EXECUTE FUNCTION app.protect_document_version()
    """)
    op.execute("""
        CREATE FUNCTION app.protect_document_section() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        DECLARE version_id uuid; edition_status text;
        BEGIN
            IF TG_OP = 'UPDATE' AND
                ROW(NEW.id, NEW.document_version_id, NEW.section_key, NEW.created_at)
                IS DISTINCT FROM
                ROW(OLD.id, OLD.document_version_id, OLD.section_key, OLD.created_at)
            THEN
                RAISE EXCEPTION 'Section identity is immutable' USING ERRCODE = '23514';
            END IF;
            IF TG_OP = 'DELETE' THEN version_id := OLD.document_version_id;
            ELSE version_id := NEW.document_version_id; END IF;
            -- Serializes every section write with publication of its parent.
            SELECT status INTO edition_status FROM app.document_versions
                WHERE id = version_id FOR UPDATE;
            IF edition_status <> 'draft' THEN
                RAISE EXCEPTION 'Only draft sections can be changed'
                    USING ERRCODE = '23514';
            END IF;
            IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER document_section_immutable BEFORE INSERT OR UPDATE OR DELETE
        ON app.document_sections FOR EACH ROW
        EXECUTE FUNCTION app.protect_document_section()
    """)
    for name in ("protect_document_version", "protect_document_section"):
        op.execute(f"REVOKE ALL ON FUNCTION app.{name}() FROM PUBLIC")


def downgrade() -> None:
    # Destructive, explicit operator action only. Never called by app or tests.
    op.drop_constraint(
        "fk_equipment_models_manual_document_id_documents",
        "equipment_models",
        schema="app",
        type_="foreignkey",
    )
    op.drop_column("equipment_models", "manual_document_id", schema="app")
    op.drop_table("document_sections", schema="app")
    op.drop_table("document_versions", schema="app")
    op.drop_table("documents", schema="app")
    op.execute("DROP FUNCTION app.protect_document_section()")
    op.execute("DROP FUNCTION app.protect_document_version()")
