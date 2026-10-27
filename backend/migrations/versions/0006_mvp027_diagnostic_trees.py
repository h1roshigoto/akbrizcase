"""MVP-027 draft diagnostic editions; preserve all existing data and history."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0006_mvp027_diagnostic_trees"
down_revision = "0005_mvp024_applicability"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "diag_trees",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.String(100), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("model_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("graph", JSONB(), nullable=False),
        sa.Column("yaml_checksum", sa.String(64), nullable=False),
        sa.Column("git_commit", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="draft"),
        sa.Column("expert_name", sa.String(200)),
        sa.Column("review_reference", sa.String(200)),
        sa.Column("approved_at", sa.DateTime(timezone=True)),
        sa.Column("published_by", sa.Uuid()),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.PrimaryKeyConstraint("id", name="pk_diag_trees"),
        sa.UniqueConstraint("key", "version", name="uq_diag_trees_key"),
        sa.ForeignKeyConstraint(
            ["model_id"],
            ["app.equipment_models.id"],
            ondelete="RESTRICT",
            name="fk_diag_trees_model_id_equipment_models",
        ),
        sa.ForeignKeyConstraint(
            ["published_by"],
            ["app.staff_users.id"],
            ondelete="RESTRICT",
            name="fk_diag_trees_published_by_staff_users",
        ),
        *[
            sa.CheckConstraint(condition, name=op.f("ck_diag_trees_" + name))
            for name, condition in (
                ("version", "version > 0"),
                ("key", "key ~ '^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$'"),
                (
                    "title",
                    "title = btrim(title) AND title <> '' AND title !~ '[[:cntrl:]]'",
                ),
                ("status", "status IN ('draft','published','retired','revoked')"),
                ("graph", "jsonb_typeof(graph) = 'object'"),
                ("yaml_checksum", "yaml_checksum ~ '^[0-9a-f]{64}$'"),
                ("git_commit", "git_commit ~ '^([0-9a-f]{40}|[0-9a-f]{64})$'"),
                (
                    "approval",
                    "(expert_name IS NULL AND review_reference IS NULL "
                    "AND approved_at IS NULL AND published_by IS NULL "
                    "AND published_at IS NULL AND status IN ('draft','revoked')) OR "
                    "(status <> 'draft' AND expert_name IS NOT NULL "
                    "AND length(expert_name) BETWEEN 3 AND 200 "
                    "AND expert_name = btrim(expert_name) "
                    "AND expert_name !~ '[[:cntrl:]]' "
                    "AND review_reference IS NOT NULL "
                    "AND review_reference ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{2,199}$' "
                    "AND approved_at IS NOT NULL AND isfinite(approved_at) "
                    "AND published_by IS NOT NULL AND published_at IS NOT NULL "
                    "AND isfinite(published_at) AND approved_at <= published_at)",
                ),
            )
        ],
        schema="app",
    )
    op.create_index(
        "ix_diag_trees_published",
        "diag_trees",
        ["key"],
        unique=True,
        postgresql_where=sa.text("status = 'published'"),
        schema="app",
    )
    for name, columns in (
        ("model_status_key", ["model_id", "status", "key"]),
        ("status_key", ["status", "key"]),
    ):
        op.create_index("ix_diag_trees_" + name, "diag_trees", columns, schema="app")
    for table, target, column in (
        ("diag_tree_sources", "document_sections", "section_id"),
        ("diag_tree_items", "catalog_items", "item_id"),
    ):
        op.create_table(
            table,
            sa.Column("tree_id", sa.Uuid(), nullable=False),
            sa.Column("node_key", sa.String(100), nullable=False),
            sa.Column(column, sa.Uuid(), nullable=False),
            *(
                [sa.Column("qty", sa.Integer(), nullable=False)]
                if table == "diag_tree_items"
                else []
            ),
            sa.PrimaryKeyConstraint("tree_id", "node_key", column, name="pk_" + table),
            sa.CheckConstraint(
                "node_key ~ '^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$'",
                name=op.f("ck_" + table + "_node_key"),
            ),
            *(
                [
                    sa.CheckConstraint(
                        "qty BETWEEN 1 AND 999", name=op.f("ck_diag_tree_items_qty")
                    )
                ]
                if table == "diag_tree_items"
                else []
            ),
            sa.ForeignKeyConstraint(
                ["tree_id"],
                ["app.diag_trees.id"],
                ondelete="RESTRICT",
                name="fk_" + table + "_tree_id_diag_trees",
            ),
            sa.ForeignKeyConstraint(
                [column],
                [f"app.{target}.id"],
                ondelete="RESTRICT",
                name="fk_" + table + "_" + column + "_" + target,
            ),
            schema="app",
        )
        op.create_index("ix_" + table + "_" + column, table, [column], schema="app")
    # Invoker rights, fixed search_path, no SECURITY DEFINER/status bypass API.
    op.execute("""
        CREATE FUNCTION app.guard_diag_tree() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, app AS $$
        BEGIN
          IF TG_OP = 'INSERT' THEN
            PERFORM pg_advisory_xact_lock(hashtextextended('diag-tree:' || NEW.key, 0));
            IF EXISTS (SELECT 1 FROM app.diag_trees
                       WHERE key = NEW.key AND model_id <> NEW.model_id) THEN
              RAISE EXCEPTION 'TREE_MODEL_CONFLICT' USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
          END IF;
          IF TG_OP = 'DELETE' THEN
            IF OLD.status <> 'draft' THEN
              RAISE EXCEPTION 'TREE_HISTORY_IMMUTABLE' USING ERRCODE = '23514';
            END IF;
            RETURN OLD;
          END IF;
          IF ROW(NEW.id,NEW.key,NEW.version,NEW.model_id)
             IS DISTINCT FROM ROW(OLD.id,OLD.key,OLD.version,OLD.model_id) THEN
            RAISE EXCEPTION 'TREE_IDENTITY_IMMUTABLE' USING ERRCODE = '23514';
          END IF;
          IF OLD.status <> 'draft' AND
             ROW(NEW.title,NEW.graph,NEW.yaml_checksum,NEW.git_commit,
                 NEW.expert_name,NEW.review_reference,NEW.approved_at,
                 NEW.published_by,NEW.published_at)
             IS DISTINCT FROM
             ROW(OLD.title,OLD.graph,OLD.yaml_checksum,OLD.git_commit,
                 OLD.expert_name,OLD.review_reference,OLD.approved_at,
                 OLD.published_by,OLD.published_at) THEN
            RAISE EXCEPTION 'TREE_HISTORY_IMMUTABLE' USING ERRCODE = '23514';
          END IF;
          IF (OLD.status <> 'draft' AND NEW.status = 'draft') OR
             (OLD.status = 'revoked' AND NEW.status <> 'revoked') THEN
            RAISE EXCEPTION 'TREE_STATUS_INVALID' USING ERRCODE = '23514';
          END IF;
          RETURN NEW;
        END $$;
        CREATE TRIGGER diag_tree_guard BEFORE INSERT OR UPDATE OR DELETE
          ON app.diag_trees FOR EACH ROW EXECUTE FUNCTION app.guard_diag_tree();
    """)
    op.execute("""
        CREATE FUNCTION app.guard_diag_tree_link() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, app AS $$
        DECLARE parent_id uuid; parent_status text;
        BEGIN
          IF TG_OP = 'UPDATE' AND
             ROW(NEW.tree_id,NEW.node_key) IS DISTINCT FROM
             ROW(OLD.tree_id,OLD.node_key) THEN
            RAISE EXCEPTION 'TREE_LINK_IDENTITY' USING ERRCODE = '23514';
          END IF;
          IF TG_OP = 'DELETE' THEN parent_id := OLD.tree_id;
          ELSE parent_id := NEW.tree_id; END IF;
          SELECT status INTO parent_status FROM app.diag_trees
            WHERE id = parent_id FOR UPDATE;
          IF parent_status IS DISTINCT FROM 'draft' THEN
            RAISE EXCEPTION 'TREE_LINK_IMMUTABLE' USING ERRCODE = '23514';
          END IF;
          IF TG_OP = 'DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
        END $$;
        CREATE TRIGGER diag_tree_source_guard BEFORE INSERT OR UPDATE OR DELETE
          ON app.diag_tree_sources FOR EACH ROW
          EXECUTE FUNCTION app.guard_diag_tree_link();
        CREATE TRIGGER diag_tree_item_guard BEFORE INSERT OR UPDATE OR DELETE
          ON app.diag_tree_items FOR EACH ROW
          EXECUTE FUNCTION app.guard_diag_tree_link();
    """)


def downgrade() -> None:
    raise RuntimeError("Diagnostic history must be preserved; downgrade unsupported")
