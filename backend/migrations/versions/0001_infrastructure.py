"""Infrastructure only: search extension and read-only schema version access."""

from alembic import op

revision = "0001_infrastructure"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public")
    op.execute("REVOKE ALL ON app.alembic_version FROM akbrizcase_runtime")
    op.execute("GRANT SELECT ON app.alembic_version TO akbrizcase_runtime")


def downgrade() -> None:
    # Do not remove a potentially shared extension or database roles on downgrade.
    pass
