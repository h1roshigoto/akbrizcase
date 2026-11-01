"""Add retention evidence and bounded scheduler; preserve immutable snapshots."""

import sqlalchemy as sa
from alembic import op

revision = "0009_mvp038_cleanup"
down_revision = "0008_mvp037_privacy"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "orders", sa.Column("terminal_at", sa.DateTime(timezone=True)), schema="app"
    )
    # Only one recorded terminal transition agreeing with the current state is
    # evidence. Missing/conflicting history stays NULL for manual review.
    op.execute("""
        UPDATE app.orders o SET terminal_at=h.at FROM (
            SELECT order_id, min(at) AS at, min(to_status) AS status
            FROM app.order_status_history WHERE to_status IN ('closed','cancelled')
            GROUP BY order_id HAVING count(*)=1
        ) h WHERE o.id=h.order_id AND o.status=h.status
            AND h.at >= o.created_at
            AND h.at <= clock_timestamp()
            AND (o.closed_at IS NULL OR o.closed_at=h.at)
    """)
    op.create_index("ix_orders_terminal_at", "orders", ["terminal_at"], schema="app")
    op.add_column(
        "email_outbox", sa.Column("failed_at", sa.DateTime(timezone=True)), schema="app"
    )
    # No backfill from next_attempt_at/updated_at: neither is completion evidence.
    op.create_check_constraint(
        "failed_at",
        "email_outbox",
        "failed_at IS NULL OR (status='failed' AND isfinite(failed_at))",
        schema="app",
    )
    op.add_column(
        "chat_sessions", sa.Column("retention_version", sa.Text()), schema="app"
    )
    op.create_foreign_key(
        "fk_chat_retention_publication",
        "chat_sessions",
        "consent_publications",
        ["retention_version"],
        ["consent_version"],
        source_schema="app",
        referent_schema="app",
        ondelete="RESTRICT",
    )
    # Historical chat policy cannot be inferred from today's latest packet.
    op.create_table(
        "deletion_journal",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("operation", sa.String(32), nullable=False),
        sa.Column("object_id", sa.Uuid(), nullable=False),
        sa.Column("consent_id", sa.Uuid()),
        sa.Column("decision_id", sa.Uuid()),
        sa.Column("policy_version", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("retain_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("applied_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(
            ["policy_version"],
            ["app.consent_publications.consent_version"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("operation", "object_id"),
        sa.CheckConstraint(
            "operation IN ('order_pii','order_delete','chat_delete',"
            "'outbox_delete','staff_session_delete','audit_delete')",
            name="operation",
        ),
        sa.CheckConstraint(
            "(operation='order_delete' AND consent_id IS NOT NULL "
            "AND decision_id IS NOT NULL) OR (operation<>'order_delete' "
            "AND consent_id IS NULL AND decision_id IS NULL)",
            name="decision",
        ),
        sa.CheckConstraint(
            "isfinite(created_at) AND isfinite(retain_until) "
            "AND retain_until >= created_at + interval '14 days' "
            "AND (applied_at IS NULL OR (isfinite(applied_at) "
            "AND applied_at >= created_at))",
            name="times",
        ),
        schema="app",
    )
    op.create_table(
        "cleanup_cursors",
        sa.Column("category", sa.String(32), primary_key=True),
        sa.Column("after_id", sa.Uuid()),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=False),
        schema="app",
    )
    op.execute("""
        CREATE FUNCTION app.protect_deletion_entry() RETURNS trigger
        LANGUAGE plpgsql SET search_path=pg_catalog AS $$
        BEGIN
            IF TG_OP='INSERT' THEN
                IF NEW.operation='order_delete'
                    AND current_user <> 'akbrizcase_migrator' THEN
                    RAISE EXCEPTION 'Protected database role required'
                        USING ERRCODE='42501';
                END IF;
                RETURN NEW;
            END IF;
            IF TG_OP='DELETE' OR ROW(NEW.id,NEW.operation,NEW.object_id,
                NEW.consent_id,NEW.decision_id,NEW.policy_version,NEW.created_at,NEW.retain_until)
                IS DISTINCT FROM ROW(OLD.id,OLD.operation,OLD.object_id,
                OLD.consent_id,OLD.decision_id,OLD.policy_version,OLD.created_at,OLD.retain_until)
                OR (OLD.applied_at IS NOT NULL
                    AND NEW.applied_at IS DISTINCT FROM OLD.applied_at)
            THEN
                RAISE EXCEPTION 'Deletion command is immutable' USING ERRCODE='23514';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute(
        "CREATE TRIGGER deletion_entry_immutable BEFORE INSERT OR UPDATE OR DELETE "
        "ON app.deletion_journal FOR EACH ROW "
        "EXECUTE FUNCTION app.protect_deletion_entry()"
    )
    op.execute("REVOKE ALL ON FUNCTION app.protect_deletion_entry() FROM PUBLIC")
    op.execute(
        "REVOKE DELETE, TRUNCATE ON app.orders, app.consents, app.deletion_journal "
        "FROM akbrizcase_runtime"
    )
    # Enforce the new invariant without rewriting historical erased records.
    op.execute("""
        ALTER TABLE app.orders ADD CONSTRAINT ck_orders_erased_fields CHECK (
            pii_erased_at IS NULL OR (contact_name IS NULL AND email IS NULL
            AND phone IS NULL AND organization_name IS NULL AND city IS NULL
            AND comment IS NULL AND chat_session_id IS NULL AND request_hash IS NULL
            AND receipt_token_hash IS NULL)) NOT VALID
    """)
    op.execute("""
        CREATE FUNCTION app.check_erased_reasons() RETURNS trigger
        LANGUAGE plpgsql SET search_path=pg_catalog AS $$
        DECLARE target uuid;
        BEGIN
            IF TG_TABLE_NAME='orders' THEN target=NEW.id;
            ELSE target=NEW.order_id; END IF;
            IF EXISTS (SELECT 1 FROM app.orders o
                JOIN app.order_status_history h ON h.order_id=o.id
                WHERE o.id=target AND o.pii_erased_at IS NOT NULL
                    AND h.reason IS NOT NULL) THEN
                RAISE EXCEPTION 'Erased order cannot retain history text'
                    USING ERRCODE='23514';
            END IF;
            RETURN NULL;
        END $$
    """)
    for table in ("orders", "order_status_history"):
        op.execute(
            f"CREATE CONSTRAINT TRIGGER erased_reasons AFTER INSERT OR UPDATE "
            f"ON app.{table} DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
            "EXECUTE FUNCTION app.check_erased_reasons()"
        )
    op.execute("REVOKE ALL ON FUNCTION app.check_erased_reasons() FROM PUBLIC")
    op.execute("""
        CREATE FUNCTION app.protect_retention_identity() RETURNS trigger
        LANGUAGE plpgsql SET search_path=pg_catalog AS $$
        BEGIN
            IF TG_TABLE_NAME='orders' THEN
                IF OLD.pii_erased_at IS NOT NULL
                    AND NEW.pii_erased_at IS DISTINCT FROM OLD.pii_erased_at THEN
                    RAISE EXCEPTION 'Erasure cannot be reversed'
                        USING ERRCODE='23514';
                END IF;
            ELSE
                IF NEW.retention_version IS DISTINCT FROM OLD.retention_version THEN
                    RAISE EXCEPTION 'Chat retention binding is immutable'
                        USING ERRCODE='23514';
                END IF;
            END IF;
            RETURN NEW;
        END $$
    """)
    for table in ("orders", "chat_sessions"):
        op.execute(
            f"CREATE TRIGGER retention_identity BEFORE UPDATE ON app.{table} "
            "FOR EACH ROW EXECUTE FUNCTION app.protect_retention_identity()"
        )
    op.execute("REVOKE ALL ON FUNCTION app.protect_retention_identity() FROM PUBLIC")


def downgrade() -> None:
    raise RuntimeError("Privacy evidence migration requires forward recovery")
