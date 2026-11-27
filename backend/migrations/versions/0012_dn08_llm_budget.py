"""Create the shared GigaChat budget without changing catalog or chat records."""

import sqlalchemy as sa
from alembic import op

revision = "0012_dn08_llm_budget"
down_revision = "0011_mvp039_http_security"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "llm_budget_attempts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("reserved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("charged_tokens", sa.BigInteger(), nullable=False),
        sa.Column("settled", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("charged_tokens >= 0", name="charge"),
        schema="app",
    )
    op.create_index(
        "ix_llm_budget_attempts_reserved_at",
        "llm_budget_attempts",
        ["reserved_at"],
        schema="app",
    )
    op.execute("REVOKE ALL ON app.llm_budget_attempts FROM PUBLIC")
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON app.llm_budget_attempts "
        "TO akbrizcase_runtime"
    )


def downgrade() -> None:
    raise RuntimeError("Destructive downgrade is not supported")
