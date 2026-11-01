"""Explicit manual basis and independent journal-policy binding; preserve v1."""

import sqlalchemy as sa
from alembic import op

revision = "0010_mvp038_journal_basis"
down_revision = "0009_mvp038_cleanup"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "deletion_journal",
        sa.Column("format", sa.Integer(), nullable=False, server_default=sa.text("1")),
        schema="app",
    )
    op.add_column(
        "deletion_journal", sa.Column("journal_policy_version", sa.Text()), schema="app"
    )
    op.create_foreign_key(
        "fk_deletion_journal_journal_policy_version_consent_publications",
        "deletion_journal",
        "consent_publications",
        ["journal_policy_version"],
        ["consent_version"],
        source_schema="app",
        referent_schema="app",
        ondelete="RESTRICT",
    )
    # Existing policy FK, decision/time CHECKs, immutable trigger and ACL remain.
    # Only the explicit v2 manual variant can omit a collection policy. It must
    # instead have an independently validated journal-policy FK and decision ID.
    op.alter_column("deletion_journal", "policy_version", nullable=True, schema="app")
    op.create_check_constraint(
        "basis",
        "deletion_journal",
        """
        (format=1 AND policy_version IS NOT NULL AND journal_policy_version IS NULL)
        OR (format=2 AND journal_policy_version IS NOT NULL AND (
            (operation='order_delete' AND policy_version IS NULL)
            OR (operation<>'order_delete' AND policy_version IS NOT NULL
                AND policy_version=journal_policy_version)))
    """,
        schema="app",
    )
    op.execute("""
        CREATE FUNCTION app.protect_deletion_basis() RETURNS trigger
        LANGUAGE plpgsql SET search_path=pg_catalog AS $$
        BEGIN
            IF ROW(NEW.format,NEW.journal_policy_version) IS DISTINCT FROM
                ROW(OLD.format,OLD.journal_policy_version) THEN
                RAISE EXCEPTION 'Deletion basis is immutable' USING ERRCODE='23514';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute(
        "CREATE TRIGGER deletion_basis_immutable BEFORE UPDATE "
        "ON app.deletion_journal FOR EACH ROW "
        "EXECUTE FUNCTION app.protect_deletion_basis()"
    )
    op.execute("REVOKE ALL ON FUNCTION app.protect_deletion_basis() FROM PUBLIC")


def downgrade() -> None:
    raise RuntimeError("Privacy evidence migration requires forward recovery")
