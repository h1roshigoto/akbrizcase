"""MVP-004 staff identities, server sessions and append-only audit.

Revision ID: 0002_mvp004_staff
Revises: 0001_infrastructure
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002_mvp004_staff"
down_revision = "0001_infrastructure"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "staff_users",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("login", sa.String(64), nullable=False),
        sa.Column("password_hash", sa.String(512), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column(
            "is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_staff_users"),
        sa.UniqueConstraint("login", name="uq_staff_users_login"),
        sa.CheckConstraint(
            "role IN ('manager', 'admin')", name=op.f("ck_staff_users_role")
        ),
        sa.CheckConstraint(
            "login ~ '^[A-Za-z0-9_.-]{3,64}$'", name=op.f("ck_staff_users_login_format")
        ),
        sa.CheckConstraint(
            "password_hash LIKE '$argon2id$%'",
            name=op.f("ck_staff_users_password_algorithm"),
        ),
        schema="app",
    )
    op.create_table(
        "staff_sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_staff_sessions"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app.staff_users.id"],
            ondelete="RESTRICT",
            name="fk_staff_sessions_user_id_staff_users",
        ),
        sa.UniqueConstraint("token_hash", name="uq_staff_sessions_token_hash"),
        sa.CheckConstraint(
            "token_hash ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_staff_sessions_token_hash_format"),
        ),
        schema="app",
    )
    op.create_index(
        "ix_staff_sessions_user_id", "staff_sessions", ["user_id"], schema="app"
    )
    op.create_index(
        "ix_staff_sessions_expires_at", "staff_sessions", ["expires_at"], schema="app"
    )
    op.create_table(
        "audit_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=True),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("entity_type", sa.String(64), nullable=False),
        sa.Column("entity_id", sa.Uuid(), nullable=True),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("result", sa.String(32), nullable=False),
        sa.Column(
            "metadata",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_audit_events"),
        sa.ForeignKeyConstraint(
            ["actor_id"],
            ["app.staff_users.id"],
            ondelete="RESTRICT",
            name="fk_audit_events_actor_id_staff_users",
        ),
        schema="app",
    )
    op.create_index(
        "ix_audit_events_actor_id", "audit_events", ["actor_id"], schema="app"
    )
    op.create_index("ix_audit_events_at", "audit_events", ["at"], schema="app")
    op.create_index(
        "ix_audit_events_entity",
        "audit_events",
        ["entity_type", "entity_id", "at"],
        schema="app",
    )
    op.create_index(
        "uq_audit_events_bootstrap_success",
        "audit_events",
        ["action"],
        unique=True,
        postgresql_where=sa.text("action = 'staff.bootstrap' AND result = 'success'"),
        schema="app",
    )
    # Override MVP-003's default CRUD grants. Runtime cannot erase the bootstrap
    # marker or identities; deactivation is the supported lifecycle operation.
    op.execute(
        "REVOKE UPDATE, DELETE, TRUNCATE ON app.audit_events FROM akbrizcase_runtime"
    )
    op.execute("REVOKE DELETE, TRUNCATE ON app.staff_users FROM akbrizcase_runtime")


def downgrade() -> None:
    # Explicit destructive rollback only; normal deploys only upgrade.
    op.drop_table("audit_events", schema="app")
    op.drop_table("staff_sessions", schema="app")
    op.drop_table("staff_users", schema="app")
