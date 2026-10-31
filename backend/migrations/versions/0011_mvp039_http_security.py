"""Add ephemeral HMAC counters; preserve all existing rows and privileges."""

import sqlalchemy as sa
from alembic import op

revision = "0011_mvp039_http_security"
down_revision = "0010_mvp038_journal_basis"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "rate_limit_buckets",
        sa.Column("scope", sa.String(16), nullable=False),
        sa.Column("subject_hash", sa.String(64), nullable=False),
        sa.Column("used", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("scope", "subject_hash"),
        sa.CheckConstraint("scope IN ('session','order','message')", name="scope"),
        sa.CheckConstraint("subject_hash ~ '^[0-9a-f]{64}$'", name="subject_hash"),
        sa.CheckConstraint("used BETWEEN 1 AND 30", name="used"),
        sa.CheckConstraint(
            "expires_at > started_at AND expires_at <= started_at + interval '1 hour'",
            name="lifetime",
        ),
        schema="app",
    )
    op.create_index(
        "ix_rate_limit_buckets_expires_at",
        "rate_limit_buckets",
        ["expires_at"],
        schema="app",
    )
    # Existing default grants already cover new app tables. Restrict explicitly:
    # no change to staff/audit/privacy rights or schema/role privileges.
    op.execute("REVOKE ALL ON app.rate_limit_buckets FROM PUBLIC")
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON app.rate_limit_buckets "
        "TO akbrizcase_runtime"
    )


def downgrade() -> None:
    raise RuntimeError("Destructive downgrade is not supported")
