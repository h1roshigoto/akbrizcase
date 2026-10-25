"""MVP-008: order snapshots, immutable consent evidence and payload-free outbox."""

import sqlalchemy as sa
from alembic import op

revision = "0003_mvp008_orders"
down_revision = "0002_mvp006_catalog"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE SEQUENCE app.order_number_seq AS bigint")
    op.create_table(
        "consent_texts",
        sa.Column("version", sa.Text(), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("checksum", sa.Text(), nullable=False),
        sa.Column("active_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("version", name=op.f("pk_consent_texts")),
        schema="app",
    )
    op.create_table(
        "consents",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("text_version", sa.Text(), nullable=False),
        sa.Column(
            "granted_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "channel",
            sa.String(length=16),
            server_default="request_form",
            nullable=False,
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "channel = 'request_form'", name=op.f("ck_consents_channel")
        ),
        sa.ForeignKeyConstraint(
            ["text_version"],
            ["app.consent_texts.version"],
            name=op.f("fk_consents_text_version_consent_texts"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_consents")),
        schema="app",
    )
    op.create_index(
        op.f("ix_consents_text_version"),
        "consents",
        ["text_version"],
        unique=False,
        schema="app",
    )
    op.create_table(
        "orders",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "number",
            sa.BigInteger(),
            server_default=sa.text("nextval('app.order_number_seq')"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("organization_name", sa.String(length=200), nullable=True),
        sa.Column("contact_name", sa.String(length=200), nullable=True),
        sa.Column("email", sa.String(length=254), nullable=True),
        sa.Column("phone", sa.String(length=32), nullable=True),
        sa.Column("city", sa.String(length=200), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column(
            "source", sa.String(length=16), server_default="site", nullable=False
        ),
        sa.Column("status", sa.String(length=16), server_default="new", nullable=False),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("consent_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.Uuid(), nullable=False),
        sa.Column("request_hash", sa.Text(), nullable=True),
        sa.Column("receipt_token_hash", sa.Text(), nullable=True),
        sa.Column("known_subtotal", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("items_total", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("delivery_status", sa.String(length=16), nullable=False),
        sa.Column("delivery_cost", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("total_amount", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column(
            "currency", sa.String(length=3), server_default="RUB", nullable=False
        ),
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
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("pii_erased_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            (
                "(delivery_status = 'not_required' AND delivery_cost IS NOT NULL "
                "AND delivery_cost = 0) OR (delivery_status = 'pending_quote' AND "
                "delivery_cost IS NULL)"
            ),
            name=op.f("ck_orders_delivery"),
        ),
        sa.CheckConstraint(
            (
                "(kind = 'support' AND known_subtotal = 0 AND items_total IS NULL "
                "AND total_amount IS NULL AND delivery_status = 'not_required') "
                "OR (kind = 'commercial' AND (items_total IS NULL OR items_total "
                "= known_subtotal) AND CASE WHEN items_total IS NOT NULL AND "
                "delivery_cost IS NOT NULL THEN total_amount IS NOT NULL AND "
                "total_amount = items_total + delivery_cost ELSE total_amount IS "
                "NULL END)"
            ),
            name=op.f("ck_orders_totals"),
        ),
        sa.CheckConstraint("currency = 'RUB'", name=op.f("ck_orders_currency")),
        sa.CheckConstraint(
            (
                "kind <> 'support' OR pii_erased_at IS NOT NULL OR (comment IS "
                "NOT NULL AND length(trim(comment)) BETWEEN 1 AND 2000)"
            ),
            name=op.f("ck_orders_support_comment"),
        ),
        sa.CheckConstraint(
            "kind IN ('commercial', 'support')", name=op.f("ck_orders_kind")
        ),
        sa.CheckConstraint(
            (
                "known_subtotal >= 0 AND known_subtotal < 'Infinity'::numeric AND "
                "(items_total IS NULL OR (items_total >= 0 AND items_total < "
                "'Infinity'::numeric)) AND (delivery_cost IS NULL OR "
                "(delivery_cost >= 0 AND delivery_cost < 'Infinity'::numeric)) "
                "AND (total_amount IS NULL OR (total_amount >= 0 AND total_amount "
                "< 'Infinity'::numeric))"
            ),
            name=op.f("ck_orders_amounts"),
        ),
        sa.CheckConstraint("source = 'site'", name=op.f("ck_orders_source")),
        sa.CheckConstraint(
            "status IN ('new', 'in_progress', 'closed', 'cancelled')",
            name=op.f("ck_orders_status"),
        ),
        sa.CheckConstraint(
            "comment IS NULL OR length(comment) <= 2000", name=op.f("ck_orders_comment")
        ),
        sa.CheckConstraint(
            (
                "pii_erased_at IS NOT NULL OR (contact_name IS NOT NULL AND "
                "length(trim(contact_name)) > 0 AND "
                "(coalesce(length(trim(email)), 0) > 0 OR "
                "coalesce(length(trim(phone)), 0) > 0) AND request_hash IS NOT "
                "NULL AND length(request_hash) > 0 AND receipt_token_hash IS NOT "
                "NULL AND length(receipt_token_hash) > 0)"
            ),
            name=op.f("ck_orders_contact_evidence"),
        ),
        sa.CheckConstraint("version > 0", name=op.f("ck_orders_version")),
        sa.ForeignKeyConstraint(
            ["consent_id"],
            ["app.consents.id"],
            name=op.f("fk_orders_consent_id_consents"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_orders")),
        sa.UniqueConstraint("consent_id", name=op.f("uq_orders_consent_id")),
        sa.UniqueConstraint("idempotency_key", name=op.f("uq_orders_idempotency_key")),
        sa.UniqueConstraint("number", name=op.f("uq_orders_number")),
        schema="app",
    )
    op.create_index(
        "ix_orders_status_created_at",
        "orders",
        ["status", sa.literal_column("created_at DESC")],
        unique=False,
        schema="app",
    )
    op.create_table(
        "order_items",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("order_id", sa.Uuid(), nullable=False),
        sa.Column("item_id", sa.Uuid(), nullable=False),
        sa.Column("qty", sa.Integer(), nullable=False),
        sa.Column("sku_snapshot", sa.String(length=200), nullable=False),
        sa.Column("name_snapshot", sa.String(length=200), nullable=False),
        sa.Column("kind_snapshot", sa.String(length=16), nullable=False),
        sa.Column("unit_snapshot", sa.String(length=200), nullable=False),
        sa.Column("price_mode_snapshot", sa.String(length=16), nullable=False),
        sa.Column(
            "unit_price_snapshot", sa.Numeric(precision=12, scale=2), nullable=True
        ),
        sa.Column("currency_snapshot", sa.String(length=3), nullable=False),
        sa.Column("tax_note_snapshot", sa.String(length=200), nullable=False),
        sa.Column("requires_shipping_snapshot", sa.Boolean(), nullable=False),
        sa.Column("catalog_revision_snapshot", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            (
                "(price_mode_snapshot = 'fixed' AND unit_price_snapshot IS NOT "
                "NULL AND unit_price_snapshot >= 0 AND unit_price_snapshot < "
                "'Infinity'::numeric) OR (price_mode_snapshot = 'on_request' AND "
                "unit_price_snapshot IS NULL)"
            ),
            name=op.f("ck_order_items_price"),
        ),
        sa.CheckConstraint(
            "currency_snapshot = 'RUB'", name=op.f("ck_order_items_currency")
        ),
        sa.CheckConstraint(
            "kind_snapshot <> 'service' OR NOT requires_shipping_snapshot",
            name=op.f("ck_order_items_shipping"),
        ),
        sa.CheckConstraint(
            "kind_snapshot IN ('product', 'service')", name=op.f("ck_order_items_kind")
        ),
        sa.CheckConstraint(
            "price_mode_snapshot IN ('fixed', 'on_request')",
            name=op.f("ck_order_items_price_mode"),
        ),
        sa.CheckConstraint(
            "catalog_revision_snapshot > 0", name=op.f("ck_order_items_revision")
        ),
        sa.CheckConstraint("qty BETWEEN 1 AND 999", name=op.f("ck_order_items_qty")),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["app.catalog_items.id"],
            name=op.f("fk_order_items_item_id_catalog_items"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["app.orders.id"],
            name=op.f("fk_order_items_order_id_orders"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_order_items")),
        sa.UniqueConstraint(
            "order_id", "item_id", name=op.f("uq_order_items_order_id")
        ),
        schema="app",
    )
    op.create_index(
        op.f("ix_order_items_item_id"),
        "order_items",
        ["item_id"],
        unique=False,
        schema="app",
    )
    op.create_index(
        op.f("ix_order_items_order_id"),
        "order_items",
        ["order_id"],
        unique=False,
        schema="app",
    )
    op.create_table(
        "order_status_history",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("order_id", sa.Uuid(), nullable=False),
        sa.Column("from_status", sa.String(length=16), nullable=True),
        sa.Column("to_status", sa.String(length=16), nullable=False),
        sa.Column("changed_by", sa.Uuid(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "from_status IN ('new', 'in_progress', 'closed', 'cancelled')",
            name=op.f("ck_order_status_history_from_status"),
        ),
        sa.CheckConstraint(
            "to_status IN ('new', 'in_progress', 'closed', 'cancelled')",
            name=op.f("ck_order_status_history_to_status"),
        ),
        sa.ForeignKeyConstraint(
            ["changed_by"],
            ["app.staff_users.id"],
            name=op.f("fk_order_status_history_changed_by_staff_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["app.orders.id"],
            name=op.f("fk_order_status_history_order_id_orders"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_order_status_history")),
        schema="app",
    )
    op.create_index(
        op.f("ix_order_status_history_changed_by"),
        "order_status_history",
        ["changed_by"],
        unique=False,
        schema="app",
    )
    op.create_index(
        "ix_order_status_history_order_id_at",
        "order_status_history",
        ["order_id", "at"],
        unique=False,
        schema="app",
    )
    op.create_table(
        "email_outbox",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("order_id", sa.Uuid(), nullable=False),
        sa.Column(
            "event_kind",
            sa.String(length=32),
            server_default="manager_notification",
            nullable=False,
        ),
        sa.Column(
            "status", sa.String(length=16), server_default="pending", nullable=False
        ),
        sa.Column(
            "attempts", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_token", sa.Uuid(), nullable=True),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            (
                "(status = 'sending' AND lease_until IS NOT NULL AND lease_token "
                "IS NOT NULL) OR (status <> 'sending' AND lease_until IS NULL AND "
                "lease_token IS NULL)"
            ),
            name=op.f("ck_email_outbox_lease"),
        ),
        sa.CheckConstraint(
            (
                "(status = 'sent' AND sent_at IS NOT NULL) OR (status <> 'sent' "
                "AND sent_at IS NULL)"
            ),
            name=op.f("ck_email_outbox_sent_at"),
        ),
        sa.CheckConstraint(
            "event_kind = 'manager_notification'",
            name=op.f("ck_email_outbox_event_kind"),
        ),
        sa.CheckConstraint(
            "last_error_code ~ '^[A-Z][A-Z0-9_]{0,63}$'",
            name=op.f("ck_email_outbox_error_code"),
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'sending', 'sent', 'failed')",
            name=op.f("ck_email_outbox_status"),
        ),
        sa.CheckConstraint("attempts >= 0", name=op.f("ck_email_outbox_attempts")),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["app.orders.id"],
            name=op.f("fk_email_outbox_order_id_orders"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_email_outbox")),
        sa.UniqueConstraint(
            "order_id", "event_kind", name=op.f("uq_email_outbox_order_id")
        ),
        schema="app",
    )
    op.create_index(
        "ix_email_outbox_pending_due",
        "email_outbox",
        ["next_attempt_at"],
        unique=False,
        schema="app",
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index(
        "ix_email_outbox_sending_lease",
        "email_outbox",
        ["lease_until"],
        unique=False,
        schema="app",
        postgresql_where=sa.text("status = 'sending'"),
    )
    # Invoker-rights triggers protect evidence even for accidental owner DML.
    # Retiring a version is allowed; replacing/deleting its identity is not.
    op.execute("""
        CREATE FUNCTION app.protect_consent_text() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'Consent versions are permanent'
                    USING ERRCODE = '23514';
            END IF;
            IF ROW(NEW.version, NEW.purpose, NEW.text, NEW.checksum, NEW.active_from)
                IS DISTINCT FROM
                ROW(OLD.version, OLD.purpose, OLD.text, OLD.checksum, OLD.active_from)
            THEN
                RAISE EXCEPTION 'Consent version content is immutable'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER consent_text_immutable BEFORE UPDATE OR DELETE
        ON app.consent_texts FOR EACH ROW EXECUTE FUNCTION app.protect_consent_text()
    """)
    op.execute("""
        CREATE FUNCTION app.protect_order_item() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
            IF NEW IS DISTINCT FROM OLD THEN
                RAISE EXCEPTION 'Order item snapshots are immutable'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER order_item_immutable BEFORE UPDATE ON app.order_items
        FOR EACH ROW EXECUTE FUNCTION app.protect_order_item()
    """)
    op.execute("""
        CREATE FUNCTION app.protect_consent_evidence() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
            IF ROW(NEW.id, NEW.text_version, NEW.granted_at, NEW.channel)
                IS DISTINCT FROM
                ROW(OLD.id, OLD.text_version, OLD.granted_at, OLD.channel)
            THEN
                RAISE EXCEPTION 'Consent acceptance evidence is immutable'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER consent_evidence_immutable BEFORE UPDATE ON app.consents
        FOR EACH ROW EXECUTE FUNCTION app.protect_consent_evidence()
    """)
    # Default table CRUD and sequence USAGE/SELECT grants remain unchanged.
    # Trigger functions are not public APIs and need no direct runtime invocation.
    for name in (
        "protect_consent_text",
        "protect_order_item",
        "protect_consent_evidence",
    ):
        op.execute(f"REVOKE ALL ON FUNCTION app.{name}() FROM PUBLIC")


def downgrade() -> None:
    # Destructive operator rollback only; never used by upgrades or acceptance.
    for table in (
        "email_outbox",
        "order_status_history",
        "order_items",
        "orders",
        "consents",
        "consent_texts",
    ):
        op.drop_table(table, schema="app")
    for name in (
        "protect_consent_text",
        "protect_order_item",
        "protect_consent_evidence",
    ):
        op.execute(f"DROP FUNCTION app.{name}()")
    op.execute("DROP SEQUENCE app.order_number_seq")
