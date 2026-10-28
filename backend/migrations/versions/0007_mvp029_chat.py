"""MVP-029 chat storage; retain orders and their independent support description."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0007_mvp029_chat"
down_revision = "0006_mvp027_diagnostic_trees"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "chat_sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("owner_token_hash", sa.String(64), nullable=False),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("tree_id", sa.Uuid()),
        sa.Column("current_node_key", sa.String(100)),
        sa.Column("state_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("outcome", sa.String(16)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "last_activity_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now() + interval '24 hours'"),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_chat_sessions"),
        sa.ForeignKeyConstraint(
            ["tree_id"],
            ["app.diag_trees.id"],
            ondelete="RESTRICT",
            name="fk_chat_sessions_tree_id_diag_trees",
        ),
        *[
            sa.CheckConstraint(condition, name=op.f("ck_chat_sessions_" + name))
            for name, condition in (
                ("owner_token_hash", "owner_token_hash ~ '^[0-9a-f]{64}$'"),
                ("mode", "mode IN ('diagnose','knowledge','commerce')"),
                ("state_version", "state_version >= 0"),
                ("outcome", "outcome IN ('resolved','handoff','unresolved')"),
                (
                    "current_node_key",
                    "current_node_key IS NULL OR (tree_id IS NOT NULL AND "
                    "current_node_key ~ '^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$')",
                ),
                ("tree_mode", "tree_id IS NULL OR mode = 'diagnose'"),
                (
                    "times",
                    "isfinite(created_at) AND isfinite(last_activity_at) "
                    "AND isfinite(expires_at) AND last_activity_at >= created_at "
                    "AND last_activity_at <= expires_at "
                    "AND expires_at = created_at + interval '24 hours'",
                ),
            )
        ],
        schema="app",
    )
    for column in ("expires_at", "tree_id"):
        op.create_index(
            "ix_chat_sessions_" + column, "chat_sessions", [column], schema="app"
        )
    op.create_table(
        "chat_messages",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("input", JSONB(), nullable=False),
        sa.Column("response", JSONB(), nullable=False),
        sa.Column("state_version_after", sa.Integer(), nullable=False),
        sa.Column("matched_by", sa.String(16), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_chat_messages"),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["app.chat_sessions.id"],
            ondelete="CASCADE",
            name="fk_chat_messages_session_id_chat_sessions",
        ),
        sa.UniqueConstraint(
            "session_id", "request_id", name="uq_chat_messages_session_id"
        ),
        *[
            sa.CheckConstraint(condition, name=op.f("ck_chat_messages_" + name))
            for name, condition in (
                ("request_hash", "request_hash ~ '^[0-9a-f]{64}$'"),
                ("input", "jsonb_typeof(input) = 'object'"),
                ("response", "jsonb_typeof(response) = 'object'"),
                ("state_version_after", "state_version_after > 0"),
                ("matched_by", "matched_by IN ('button','keyword','search','none')"),
                ("latency_ms", "latency_ms >= 0"),
                ("created_at", "isfinite(created_at)"),
            )
        ],
        schema="app",
    )
    op.create_index(
        "ix_chat_messages_session_id_created_at",
        "chat_messages",
        ["session_id", "created_at"],
        schema="app",
    )
    op.add_column("orders", sa.Column("chat_session_id", sa.Uuid()), schema="app")
    op.create_foreign_key(
        "fk_orders_chat_session_id_chat_sessions",
        "orders",
        "chat_sessions",
        ["chat_session_id"],
        ["id"],
        source_schema="app",
        referent_schema="app",
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_orders_chat_session_id", "orders", ["chat_session_id"], schema="app"
    )
    # Storage accepts the final source enum. HTTP creation still rejects chat
    # until ownership verification is implemented by MVP-036.
    op.drop_constraint(op.f("ck_orders_source"), "orders", type_="check", schema="app")
    op.create_check_constraint(
        op.f("ck_orders_source"), "orders", "source IN ('site', 'chat')", schema="app"
    )


def downgrade() -> None:
    raise RuntimeError(
        "Chat and order history must be preserved; downgrade unsupported"
    )
