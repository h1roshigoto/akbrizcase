"""MVP-008 storage invariants, exercised directly on genuine PostgreSQL 16."""

from collections.abc import Iterator
from datetime import timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, func, insert, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from app.core.db import Database
from app.core.db_types import utc_now
from app.core.staff_models import StaffUser
from app.modules.catalog.models import CatalogItem
from app.modules.catalog.seed import SeedRefused
from app.modules.notify.models import EmailOutbox
from app.modules.orders.models import (
    Consent,
    ConsentText,
    Order,
    OrderItem,
    OrderStatusHistory,
)
from app.modules.orders.seed import DEMO_TEXT, DEMO_VERSION, seed_consent

pytestmark = pytest.mark.integration


@pytest.fixture
def orders_database(databases: tuple[Database, Database]) -> Iterator[Database]:
    runtime, migrator = databases
    try:
        yield runtime
    finally:
        with migrator.unit_of_work() as session:
            session.execute(delete(Order))  # Approved cascade, then consent.
            session.execute(delete(Consent))
            session.execute(delete(CatalogItem))
            session.execute(delete(StaffUser))
        # Consent versions are deliberately permanent, even without references.


def consent_text(session: Session) -> str:
    version = "SYNTHETIC-TEST-" + uuid4().hex
    session.add(
        ConsentText(
            version=version,
            purpose="SYNTHETIC TEST ONLY",
            text="Not legally approved",
            checksum="synthetic-checksum",
            active_from=utc_now(),
        )
    )
    session.flush()
    return version


def acceptance(session: Session) -> UUID:
    row = Consent(text_version=consent_text(session))
    session.add(row)
    session.flush()
    return row.id


def order_values(consent_id: UUID, **changes: object) -> dict[str, object]:
    return (
        dict(
            kind="commercial",
            consent_id=consent_id,
            idempotency_key=uuid4(),
            contact_name="Synthetic test contact",
            email="synthetic@example.invalid",
            request_hash="synthetic-hmac",
            receipt_token_hash="synthetic-token-hash",
            known_subtotal=Decimal("123.45"),
            items_total=Decimal("123.45"),
            delivery_status="not_required",
            delivery_cost=Decimal("0.00"),
            total_amount=Decimal("123.45"),
        )
        | changes
    )


def new_order(session: Session, **changes: object) -> Order:
    row = session.scalars(
        insert(Order)
        .values(**order_values(acceptance(session), **changes))
        .returning(Order)
    ).one()
    return row


def item_values(order_id: UUID, item_id: UUID, **changes: object) -> dict[str, object]:
    return (
        dict(
            order_id=order_id,
            item_id=item_id,
            qty=2,
            sku_snapshot="SYNTHETIC-SKU",
            name_snapshot="Synthetic item",
            kind_snapshot="product",
            unit_snapshot="pc",
            price_mode_snapshot="fixed",
            unit_price_snapshot=Decimal("123.45"),
            currency_snapshot="RUB",
            tax_note_snapshot="SYNTHETIC TAX NOTE",
            requires_shipping_snapshot=False,
            catalog_revision_snapshot=1,
        )
        | changes
    )


def catalog_item(session: Session) -> UUID:
    row = CatalogItem(
        sku="SYNTHETIC-" + uuid4().hex,
        kind="product",
        name="Synthetic item",
        description="Test only",
        unit="pc",
        price_mode="fixed",
        price=Decimal("123.45"),
        tax_note="Synthetic",
        status="published",
    )
    session.add(row)
    session.flush()
    return row.id


@pytest.mark.parametrize("phone_only", [False, True])
def test_generated_values_and_runtime_crud(
    orders_database: Database, phone_only: bool
) -> None:
    with orders_database.unit_of_work() as session:
        changes = {"email": None, "phone": "+70000000000"} if phone_only else {}
        first = new_order(session, **changes)
        second = new_order(session)
        assert first.number != second.number
        for row in (first, second):
            assert isinstance(row.id, UUID) and row.id.version == 4
            assert isinstance(row.number, int)
            assert row.known_subtotal == Decimal("123.45")
            assert isinstance(row.known_subtotal, Decimal)
            assert row.currency == "RUB" and row.version == 1
            assert row.status == "new" and row.source == "site"
            for at in (row.created_at, row.updated_at):
                assert at.utcoffset() == timedelta(0)
            consent = session.get(Consent, row.consent_id)
            assert consent is not None and consent.granted_at.utcoffset() == timedelta(
                0
            )
        first.status = "in_progress"
    with orders_database.unit_of_work() as session:
        assert session.get(Order, first.id).status == "in_progress"  # type: ignore[union-attr]
        session.execute(delete(Order).where(Order.id == first.id))


@pytest.mark.parametrize(
    "changes",
    [
        {"contact_name": None},
        {"contact_name": "  "},
        {"email": None},
        {"email": "  ", "phone": "  "},
        {"request_hash": None},
        {"receipt_token_hash": None},
        {"request_hash": ""},
        {"receipt_token_hash": ""},
    ],
)
def test_contact_evidence_required_before_erasure(
    orders_database: Database, changes: dict[str, object]
) -> None:
    with pytest.raises(IntegrityError, match="ck_orders_contact_evidence"):
        with orders_database.unit_of_work() as session:
            new_order(session, **changes)


@pytest.mark.parametrize("kind", ["commercial", "support"])
def test_atomic_erasure_keeps_order_and_consent(
    orders_database: Database, kind: str
) -> None:
    with orders_database.unit_of_work() as session:
        extra = (
            dict(known_subtotal=0, items_total=None, total_amount=None)
            if kind == "support"
            else {}
        )
        row = new_order(
            session, kind=kind, comment="Synthetic support description", **extra
        )
        identifier, consent_id = row.id, row.consent_id
        session.add(
            OrderStatusHistory(
                order_id=row.id, to_status="new", reason="Synthetic reason"
            )
        )
    with pytest.raises(IntegrityError):
        with orders_database.unit_of_work() as session:
            session.execute(update(Order).values(comment=None, contact_name=None))
    with orders_database.unit_of_work() as session:
        session.execute(
            update(Order)
            .where(Order.id == identifier)
            .values(
                pii_erased_at=utc_now(),
                organization_name=None,
                contact_name=None,
                email=None,
                phone=None,
                city=None,
                comment=None,
                request_hash=None,
                receipt_token_hash=None,
            )
        )
        session.execute(update(OrderStatusHistory).values(reason=None))
    with orders_database.unit_of_work() as session:
        erased = session.get(Order, identifier)
        assert erased is not None and erased.pii_erased_at is not None
        assert (
            erased.contact_name
            is erased.comment
            is erased.request_hash
            is erased.receipt_token_hash
            is None
        )
        assert session.get(Consent, consent_id) is not None
        assert session.scalar(select(OrderStatusHistory.reason)) is None


@pytest.mark.parametrize("comment", [None, "", "   ", "x" * 2001])
def test_support_requires_meaningful_bounded_comment(
    orders_database: Database, comment: str | None
) -> None:
    with pytest.raises(IntegrityError, match="ck_orders_(support_comment|comment)"):
        with orders_database.unit_of_work() as session:
            new_order(
                session,
                kind="support",
                comment=comment,
                known_subtotal=0,
                items_total=None,
                total_amount=None,
            )


@pytest.mark.parametrize("comment", ["  x  ", "x" * 2000])
def test_valid_support_comment(orders_database: Database, comment: str) -> None:
    with orders_database.unit_of_work() as session:
        row = new_order(
            session,
            kind="support",
            comment=comment,
            known_subtotal=0,
            items_total=None,
            total_amount=None,
        )
        assert row.comment == comment


@pytest.mark.parametrize(
    "changes",
    [
        {"source": "chat"},
        {"source": "other"},
        {"kind": "other"},
        {"status": "other"},
        {"version": 0},
        {"version": -1},
        {"currency": "USD"},
        {"known_subtotal": Decimal("-0.01")},
        {"known_subtotal": Decimal("NaN")},
        {"items_total": Decimal("NaN")},
        {"total_amount": Decimal("NaN")},
        {"delivery_cost": Decimal("NaN")},
        {"known_subtotal": Decimal("Infinity")},
        {"delivery_status": "other"},
        {"delivery_cost": None},
        {"delivery_cost": 1},
        {"delivery_cost": -1},
        {"delivery_status": "pending_quote"},
        {"delivery_status": "pending_quote", "delivery_cost": None},
        {"items_total": None},
        {"total_amount": None},
        {"items_total": 0},
        {"total_amount": 0},
        {"items_total": -1},
        {"total_amount": -1},
        {"kind": "support", "comment": "Synthetic description"},
        {
            "kind": "support",
            "comment": "Synthetic description",
            "items_total": None,
            "total_amount": None,
        },
        {
            "kind": "support",
            "comment": "Synthetic description",
            "known_subtotal": 0,
            "items_total": None,
            "total_amount": None,
            "delivery_status": "pending_quote",
            "delivery_cost": None,
        },
    ],
)
def test_invalid_order_shapes_and_finances(
    orders_database: Database, changes: dict[str, object]
) -> None:
    with pytest.raises(DBAPIError):
        with orders_database.unit_of_work() as session:
            new_order(session, **changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"known_subtotal": 0, "items_total": 0, "total_amount": 0},
        {"items_total": None, "total_amount": None},
        {
            "delivery_status": "pending_quote",
            "delivery_cost": None,
            "total_amount": None,
        },
        {
            "items_total": None,
            "delivery_status": "pending_quote",
            "delivery_cost": None,
            "total_amount": None,
        },
    ],
)
def test_unknown_amounts_stay_null_and_known_zero_is_valid(
    orders_database: Database, changes: dict[str, object]
) -> None:
    with orders_database.unit_of_work() as session:
        row = new_order(session, **changes)
        assert row.total_amount == changes["total_amount"]
        assert row.items_total == changes.get("items_total", Decimal("123.45"))


@pytest.mark.parametrize("column", ["number", "idempotency_key", "consent_id"])
def test_order_uniques(orders_database: Database, column: str) -> None:
    with orders_database.unit_of_work() as session:
        first = new_order(session)
        value = getattr(first, column)
    with pytest.raises(IntegrityError, match=f"uq_orders_{column}"):
        with orders_database.unit_of_work() as session:
            values = order_values(acceptance(session)) | {column: value}
            session.execute(insert(Order).values(**values))


@pytest.mark.parametrize("referenced", [False, True])
@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE app.consent_texts SET text = 'changed' WHERE version = :version",
        "UPDATE app.consent_texts SET checksum = 'changed' WHERE version = :version",
        "UPDATE app.consent_texts SET version = 'reused' WHERE version = :version",
        "UPDATE app.consent_texts SET purpose = 'changed' WHERE version = :version",
        "UPDATE app.consent_texts SET active_from = now() + interval '1 day' "
        "WHERE version = :version",
        "DELETE FROM app.consent_texts WHERE version = :version",
    ],
)
def test_consent_versions_immutable_even_for_migrator(
    databases: tuple[Database, Database],
    orders_database: Database,
    referenced: bool,
    statement: str,
) -> None:
    with orders_database.unit_of_work() as session:
        version = consent_text(session)
        if referenced:
            consent = Consent(text_version=version)
            session.add(consent)
            session.flush()
            session.execute(insert(Order).values(**order_values(consent.id)))
    for database in databases:
        with pytest.raises(IntegrityError):
            with database.unit_of_work() as session:
                session.execute(text(statement), {"version": version})
    with orders_database.unit_of_work() as session:
        session.execute(
            update(ConsentText)
            .where(ConsentText.version == version)
            .values(retired_at=utc_now())
        )
    # Reusing an existing version cannot replace its contents.
    with pytest.raises(IntegrityError, match="pk_consent_texts"):
        with orders_database.unit_of_work() as session:
            session.execute(
                insert(ConsentText).values(
                    version=version,
                    purpose="Test",
                    text="Conflicting",
                    checksum="other",
                    active_from=utc_now(),
                )
            )


@pytest.mark.parametrize(
    "changes",
    [{"text_version": "missing"}, {"granted_at": utc_now() + timedelta(days=1)}],
)
def test_consent_acceptance_evidence_immutable(
    orders_database: Database, changes: dict[str, object]
) -> None:
    with orders_database.unit_of_work() as session:
        identifier = acceptance(session)
    with pytest.raises(IntegrityError):
        with orders_database.unit_of_work() as session:
            session.execute(
                update(Consent).where(Consent.id == identifier).values(**changes)
            )
    with orders_database.unit_of_work() as session:
        session.execute(
            update(Consent).where(Consent.id == identifier).values(revoked_at=utc_now())
        )


@pytest.mark.parametrize("values", [{"text_version": "missing"}, {"channel": "chat"}])
def test_consent_fk_and_channel(
    orders_database: Database, values: dict[str, object]
) -> None:
    with pytest.raises(IntegrityError):
        with orders_database.unit_of_work() as session:
            session.execute(
                insert(Consent).values(
                    **({"text_version": consent_text(session)} | values)
                )
            )


@pytest.mark.parametrize(
    "changes",
    [
        {"qty": 0},
        {"qty": 1000},
        {"catalog_revision_snapshot": 0},
        {"catalog_revision_snapshot": -1},
        {"kind_snapshot": "other"},
        {"currency_snapshot": "USD"},
        {"price_mode_snapshot": "other"},
        {"unit_price_snapshot": None},
        {"unit_price_snapshot": Decimal("-0.01")},
        {"unit_price_snapshot": Decimal("NaN")},
        {"unit_price_snapshot": Decimal("Infinity")},
        {"price_mode_snapshot": "on_request"},
        {"kind_snapshot": "service", "requires_shipping_snapshot": True},
    ],
)
def test_invalid_snapshots(
    orders_database: Database, changes: dict[str, object]
) -> None:
    with pytest.raises(DBAPIError):
        with orders_database.unit_of_work() as session:
            row = new_order(session)
            session.execute(
                insert(OrderItem).values(
                    **item_values(row.id, catalog_item(session), **changes)
                )
            )


@pytest.mark.parametrize("kind", ["product", "service"])
@pytest.mark.parametrize("price", [None, Decimal("0.00"), Decimal("123.45")])
@pytest.mark.parametrize("qty", [1, 999])
def test_snapshots_valid_immutable_and_independent_of_catalog(
    orders_database: Database,
    kind: str,
    price: Decimal | None,
    qty: int,
) -> None:
    with orders_database.unit_of_work() as session:
        row = new_order(session)
        item_id = catalog_item(session)
        values = item_values(
            row.id,
            item_id,
            kind_snapshot=kind,
            unit_price_snapshot=price,
            qty=qty,
            price_mode_snapshot="on_request" if price is None else "fixed",
            requires_shipping_snapshot=kind == "product",
        )
        identifier = session.scalar(
            insert(OrderItem).values(**values).returning(OrderItem.id)
        )
        session.execute(
            update(CatalogItem)
            .where(CatalogItem.id == item_id)
            .values(
                price=999,
                revision=2,
                status="archived",
                name="Changed catalog name",
            )
        )
    with orders_database.unit_of_work() as session:
        snapshot = session.get(OrderItem, identifier)
        assert snapshot is not None
        assert snapshot.unit_price_snapshot == price
        assert snapshot.catalog_revision_snapshot == 1
        assert snapshot.name_snapshot == "Synthetic item"
    for column, value in (
        ("qty", 3),
        ("name_snapshot", "Other"),
        ("unit_price_snapshot", 3),
    ):
        with pytest.raises(IntegrityError, match="immutable"):
            with orders_database.unit_of_work() as session:
                session.execute(
                    update(OrderItem)
                    .where(OrderItem.id == identifier)
                    .values(**{column: value})
                )
    with pytest.raises(IntegrityError, match="uq_order_items_order_id"):
        with orders_database.unit_of_work() as session:
            session.execute(insert(OrderItem).values(**values))


@pytest.mark.parametrize(
    "changes",
    [
        {"status": "other"},
        {"event_kind": "other"},
        {"attempts": -1},
        {"status": "sending"},
        {"lease_token": uuid4()},
        {"lease_until": utc_now()},
        {"status": "sending", "lease_token": uuid4()},
        {"status": "sending", "lease_until": utc_now()},
        {"status": "sent"},
        {"sent_at": utc_now()},
        {"last_error_code": "SMTP error for synthetic@example.invalid"},
        {"last_error_code": ""},
    ],
)
def test_invalid_outbox(orders_database: Database, changes: dict[str, object]) -> None:
    with pytest.raises(IntegrityError):
        with orders_database.unit_of_work() as session:
            session.execute(
                insert(EmailOutbox).values(order_id=new_order(session).id, **changes)
            )


@pytest.mark.parametrize("status", ["pending", "sending", "sent", "failed"])
def test_outbox_valid_states_and_unique_event(
    orders_database: Database, status: str
) -> None:
    with orders_database.unit_of_work() as session:
        row = new_order(session)
        extra: dict[str, object] = {}
        if status == "sending":
            extra = dict(
                lease_token=uuid4(),
                lease_until=utc_now() + timedelta(minutes=2),
                attempts=1,
            )
        elif status == "sent":
            extra = dict(sent_at=utc_now(), attempts=1)
        elif status == "failed":
            extra = dict(attempts=5, last_error_code="SMTP_TIMEOUT")
        outbox = session.scalars(
            insert(EmailOutbox)
            .values(order_id=row.id, status=status, **extra)
            .returning(EmailOutbox)
        ).one()
        assert isinstance(outbox.id, UUID) and outbox.id.version == 4
        assert outbox.next_attempt_at.utcoffset() == timedelta(0)
        assert outbox.created_at.utcoffset() == timedelta(0)
        assert outbox.event_kind == "manager_notification"
        assert outbox.attempts == extra.get("attempts", 0)
    with pytest.raises(IntegrityError, match="uq_email_outbox_order_id"):
        with orders_database.unit_of_work() as session:
            session.add(EmailOutbox(order_id=row.id))


@pytest.mark.parametrize(
    "changes",
    [{"from_status": "other"}, {"to_status": "other"}, {"changed_by": uuid4()}],
)
def test_history_statuses_and_staff_fk(
    orders_database: Database, changes: dict[str, object]
) -> None:
    with pytest.raises(IntegrityError):
        with orders_database.unit_of_work() as session:
            session.execute(
                insert(OrderStatusHistory).values(
                    **(dict(order_id=new_order(session).id, to_status="new") | changes)
                )
            )


def test_restrict_references_and_approved_order_deletion(
    databases: tuple[Database, Database], orders_database: Database
) -> None:
    with orders_database.unit_of_work() as session:
        row = new_order(session)
        consent_id, identifier = row.consent_id, row.id
        item_id = catalog_item(session)
        session.execute(insert(OrderItem).values(**item_values(row.id, item_id)))
        staff = StaffUser(
            login="synthetic-history-staff",
            password_hash="$argon2id$synthetic",
            role="manager",
        )
        session.add(staff)
        session.flush()
        staff_id = staff.id
        session.add(OrderStatusHistory(order_id=row.id, to_status="new"))
        session.add(
            OrderStatusHistory(
                order_id=row.id,
                from_status="new",
                to_status="in_progress",
                changed_by=staff.id,
                reason="Synthetic reason",
            )
        )
        session.add(EmailOutbox(order_id=row.id))
    # Migrator bypasses runtime staff DELETE denial: this proves the actual FK.
    for statement in (
        delete(Consent).where(Consent.id == consent_id),
        delete(CatalogItem).where(CatalogItem.id == item_id),
        delete(StaffUser).where(StaffUser.id == staff_id),
    ):
        with pytest.raises(IntegrityError):
            with databases[1].unit_of_work() as session:
                session.execute(statement)
    with orders_database.unit_of_work() as session:
        session.execute(delete(Order).where(Order.id == identifier))
        for child in (OrderItem, OrderStatusHistory, EmailOutbox):
            assert session.scalar(select(func.count()).select_from(child)) == 0
        assert session.get(Consent, consent_id) is not None
        session.execute(delete(Consent).where(Consent.id == consent_id))
        assert session.get(CatalogItem, item_id) is not None
        assert session.get(StaffUser, staff_id) is not None


@pytest.mark.parametrize("model", [OrderItem, OrderStatusHistory, EmailOutbox])
def test_children_require_existing_order(
    orders_database: Database,
    model: type[OrderItem] | type[OrderStatusHistory] | type[EmailOutbox],
) -> None:
    with pytest.raises(IntegrityError):
        with orders_database.unit_of_work() as session:
            if model is OrderItem:
                values = item_values(uuid4(), catalog_item(session))
            elif model is OrderStatusHistory:
                values = dict(order_id=uuid4(), to_status="new")
            else:
                values = dict(order_id=uuid4())
            session.execute(insert(model).values(**values))


def test_order_requires_existing_consent(orders_database: Database) -> None:
    with pytest.raises(IntegrityError, match="fk_orders_consent_id_consents"):
        with orders_database.unit_of_work() as session:
            session.execute(insert(Order).values(**order_values(uuid4())))


def test_atomic_rollback_of_future_creation_sequence(orders_database: Database) -> None:
    with pytest.raises(IntegrityError):
        with orders_database.unit_of_work() as session:
            row = new_order(session)
            session.execute(
                insert(OrderItem).values(**item_values(row.id, catalog_item(session)))
            )
            session.add(OrderStatusHistory(order_id=row.id, to_status="new"))
            session.add(EmailOutbox(order_id=row.id))
            session.flush()
            # A late duplicate event must undo every earlier row, including consent.
            session.add(EmailOutbox(order_id=row.id))
    with orders_database.unit_of_work() as session:
        for model in (
            Order,
            Consent,
            OrderItem,
            OrderStatusHistory,
            EmailOutbox,
            CatalogItem,
        ):
            assert session.scalar(select(func.count()).select_from(model)) == 0
        assert orders_database.ready()


def test_outbox_indexes_permissions_and_no_payload(orders_database: Database) -> None:
    with orders_database.engine.connect() as connection:
        indexes = dict(
            connection.execute(
                text(
                    "SELECT indexname, indexdef FROM pg_indexes "
                    "WHERE schemaname = 'app' "
                    "AND tablename IN ('orders', 'order_items', "
                    "'order_status_history', 'email_outbox')"
                )
            )
            .tuples()
            .all()
        )
        for name, column, status in (
            ("ix_email_outbox_pending_due", "next_attempt_at", "pending"),
            ("ix_email_outbox_sending_lease", "lease_until", "sending"),
        ):
            assert f"USING btree ({column})" in indexes[name]
            assert "WHERE" in indexes[name] and status in indexes[name]
            connection.execute(text("SET LOCAL enable_seqscan = off"))
            plan = " ".join(
                connection.scalars(
                    text(
                        "EXPLAIN SELECT id FROM app.email_outbox "
                        f"WHERE status = '{status}' "
                        f"AND {column} <= now() ORDER BY {column} LIMIT 1"
                    )
                )
            )
            assert name in plan
        assert "status, created_at DESC" in indexes["ix_orders_status_created_at"]
        assert "order_id, at" in indexes["ix_order_status_history_order_id_at"]
        assert "order_id" in indexes["ix_order_items_order_id"]
        for table in (
            "consent_texts",
            "consents",
            "orders",
            "order_items",
            "order_status_history",
            "email_outbox",
        ):
            assert (
                connection.scalar(
                    text(
                        "SELECT has_table_privilege(current_user, :table, "
                        "'SELECT,INSERT,UPDATE,DELETE')"
                    ),
                    {"table": "app." + table},
                )
                is True
            )
        assert (
            connection.scalar(
                text(
                    "SELECT has_sequence_privilege(current_user, "
                    "'app.order_number_seq', 'USAGE,SELECT')"
                )
            )
            is True
        )
        assert (
            connection.scalar(
                text(
                    "SELECT has_sequence_privilege(current_user, "
                    "'app.order_number_seq', 'UPDATE')"
                )
            )
            is False
        )
        columns = set(
            connection.scalars(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = 'app' AND table_name = 'email_outbox'"
                )
            )
        )
        assert columns == {
            "id",
            "order_id",
            "event_kind",
            "status",
            "attempts",
            "next_attempt_at",
            "lease_until",
            "lease_token",
            "last_error_code",
            "created_at",
            "sent_at",
        }
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM information_schema.columns "
                    "WHERE table_schema='app' "
                    "AND table_name='orders' AND column_name='chat_session_id'"
                )
            )
            == 0
        )


def test_synthetic_consent_seed_guards_repeat_and_conflicts(
    orders_database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    for mode in ("demo", "production"):
        monkeypatch.setenv("APP_ENV", mode)
        with pytest.raises(SeedRefused):
            seed_consent(orders_database, mode=mode)
    monkeypatch.setenv("APP_ENV", "test")
    seed_consent(orders_database, mode="test")
    seed_consent(orders_database, mode="test")
    with orders_database.unit_of_work() as session:
        row = session.get(ConsentText, DEMO_VERSION)
        assert row is not None and row.text == DEMO_TEXT
        assert "NOT LEGALLY APPROVED" in row.purpose
        assert "Not legally approved" in row.text
        assert (
            session.scalar(
                select(func.count())
                .select_from(ConsentText)
                .where(ConsentText.version == DEMO_VERSION)
            )
            == 1
        )
    # A conflicting existing identifier is refused, rather than silently reused.
    monkeypatch.setattr(
        "app.modules.orders.seed.DEMO_TEXT", "Conflicting synthetic text"
    )
    with pytest.raises(SeedRefused, match="Conflicting"):
        seed_consent(orders_database, mode="test")


@pytest.mark.parametrize(
    "column",
    [
        "consent_id",
        "idempotency_key",
        "kind",
        "source",
        "status",
        "version",
        "known_subtotal",
        "delivery_status",
        "currency",
        "created_at",
        "updated_at",
    ],
)
def test_required_order_fields_cannot_be_null(
    orders_database: Database, column: str
) -> None:
    with pytest.raises(IntegrityError):
        with orders_database.unit_of_work() as session:
            values = order_values(acceptance(session)) | {column: None}
            session.execute(insert(Order).values(**values))


@pytest.mark.parametrize(
    "column", ["status", "attempts", "event_kind", "next_attempt_at", "created_at"]
)
def test_required_outbox_fields_cannot_be_null(
    orders_database: Database, column: str
) -> None:
    with pytest.raises(IntegrityError):
        with orders_database.unit_of_work() as session:
            session.execute(
                insert(EmailOutbox).values(
                    order_id=new_order(session).id, **{column: None}
                )
            )


def test_snapshot_requires_existing_catalog_item(orders_database: Database) -> None:
    with pytest.raises(IntegrityError, match="fk_order_items_item_id_catalog_items"):
        with orders_database.unit_of_work() as session:
            session.execute(
                insert(OrderItem).values(**item_values(new_order(session).id, uuid4()))
            )


def order_bundle(session: Session, *, erased: bool = False) -> tuple[UUID, UUID]:
    """Persist a complete synthetic graph without a future order service."""
    row = new_order(session)
    for _ in range(2):
        session.execute(
            insert(OrderItem).values(**item_values(row.id, catalog_item(session)))
        )
    session.add(OrderStatusHistory(order_id=row.id, to_status="new"))
    session.add(EmailOutbox(order_id=row.id))
    if erased:
        row.pii_erased_at = utc_now()
        row.contact_name = row.email = row.request_hash = row.receipt_token_hash = None
    session.flush()
    return row.id, row.consent_id


@pytest.mark.parametrize("role", [0, 1], ids=["runtime", "migrator"])
@pytest.mark.parametrize("erased", [False, True], ids=["retained-pii", "erased-pii"])
@pytest.mark.parametrize("scope", ["id", "order_id"])
def test_standalone_snapshot_delete_rejected_and_transaction_rolled_back(
    databases: tuple[Database, Database],
    orders_database: Database,
    role: int,
    erased: bool,
    scope: str,
) -> None:
    with orders_database.unit_of_work() as session:
        order_id, consent_id = order_bundle(session, erased=erased)
        before = session.execute(
            select(OrderItem.__table__)
            .where(OrderItem.order_id == order_id)
            .order_by(OrderItem.id)
        ).all()
        item_id = before[0].id
    # Raw SQL bypasses ORM protections. A bulk DELETE is forbidden as well.
    with pytest.raises(IntegrityError, match="cannot be deleted independently"):
        with databases[role].unit_of_work() as session:
            session.execute(
                update(Order)
                .where(Order.id == order_id)
                .values(
                    status="in_progress",
                    version=2,
                )
            )
            session.execute(
                text(f"DELETE FROM app.order_items WHERE {scope} = :id"),
                {"id": item_id if scope == "id" else order_id},
            )
    with orders_database.unit_of_work() as session:
        assert (
            session.execute(
                select(OrderItem.__table__)
                .where(OrderItem.order_id == order_id)
                .order_by(OrderItem.id)
            ).all()
            == before
        )
        order = session.get(Order, order_id)
        assert order is not None and order.status == "new" and order.version == 1
        assert order.consent_id == consent_id
        for model in (OrderStatusHistory, EmailOutbox):
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(model)
                    .where(model.order_id == order_id)
                )
                == 1
            )


@pytest.mark.parametrize("role", [0, 1], ids=["runtime", "migrator"])
def test_cascade_deletion_and_late_fk_failure_rollback(
    databases: tuple[Database, Database],
    orders_database: Database,
    role: int,
) -> None:
    with orders_database.unit_of_work() as session:
        order_id, consent_id = order_bundle(session)
        retained_id, _ = order_bundle(session)
    database = databases[role]
    with pytest.raises(IntegrityError, match="fk_orders_consent_id_consents"):
        with database.unit_of_work() as session:
            session.execute(delete(Consent).where(Consent.id == consent_id))
    with pytest.raises(IntegrityError, match="fk_order_status_history_order_id_orders"):
        with database.unit_of_work() as session:
            session.execute(delete(Order).where(Order.id == order_id))
            for model in (OrderItem, OrderStatusHistory, EmailOutbox):
                assert (
                    session.scalar(
                        select(func.count())
                        .select_from(model)
                        .where(model.order_id == order_id)
                    )
                    == 0
                )
            session.execute(delete(Consent).where(Consent.id == consent_id))
            # A late FK failure restores the parent, all cascaded rows and consent.
            session.add(OrderStatusHistory(order_id=order_id, to_status="new"))
    with database.unit_of_work() as session:
        assert session.get(Order, order_id) is not None
        assert session.get(Consent, consent_id) is not None
        for model, count in ((OrderItem, 2), (OrderStatusHistory, 1), (EmailOutbox, 1)):
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(model)
                    .where(model.order_id == order_id)
                )
                == count
            )
        session.execute(delete(Order).where(Order.id == order_id))
        for model in (OrderItem, OrderStatusHistory, EmailOutbox):
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(model)
                    .where(model.order_id == order_id)
                )
                == 0
            )
            remaining = session.scalar(
                select(func.count())
                .select_from(model)
                .where(model.order_id == retained_id)
            )
            assert remaining is not None and remaining > 0
        assert session.get(Consent, consent_id) is not None
        session.execute(delete(Consent).where(Consent.id == consent_id))
    with orders_database.unit_of_work() as session:
        assert session.get(Order, order_id) is None
        assert session.get(Consent, consent_id) is None
        assert session.get(Order, retained_id) is not None
        assert session.scalar(select(func.count()).select_from(CatalogItem)) == 4


@pytest.mark.parametrize("role", [0, 1], ids=["runtime", "migrator"])
@pytest.mark.parametrize("erased", [False, True], ids=["retained-pii", "erased-pii"])
@pytest.mark.parametrize("column", ["id", "number", "idempotency_key", "consent_id"])
def test_order_identity_and_consent_cannot_be_replaced(
    databases: tuple[Database, Database],
    orders_database: Database,
    role: int,
    erased: bool,
    column: str,
) -> None:
    with orders_database.unit_of_work() as session:
        # No children: changing id would otherwise pass all existing FKs.
        row = new_order(session)
        original = (row.id, row.number, row.idempotency_key, row.consent_id)
        alternate_consent = acceptance(session)
        if erased:
            row.pii_erased_at = utc_now()
            row.contact_name = row.email = row.request_hash = row.receipt_token_hash = (
                None
            )
        replacement = {
            "id": uuid4(),
            "number": row.number + 1000000,
            "idempotency_key": uuid4(),
            "consent_id": alternate_consent,
        }[column]
    # All replacements are otherwise valid and unused: this tests immutability,
    # not a coincidental UNIQUE/FK violation, including consent after erasure.
    with pytest.raises(IntegrityError, match="Order identity is immutable"):
        with databases[role].unit_of_work() as session:
            session.execute(
                update(Order)
                .where(Order.id == original[0])
                .values(**{column: replacement})
            )
    with orders_database.unit_of_work() as session:
        order = session.get(Order, original[0])
        assert order is not None
        assert (
            order.id,
            order.number,
            order.idempotency_key,
            order.consent_id,
        ) == original
        assert session.get(Consent, alternate_consent) is not None
    with pytest.raises(IntegrityError, match="fk_orders_consent_id_consents"):
        with orders_database.unit_of_work() as session:
            session.execute(delete(Consent).where(Consent.id == original[3]))


def test_identity_noop_allows_status_updates_and_erasure(
    orders_database: Database,
) -> None:
    with orders_database.unit_of_work() as session:
        row = new_order(session)
        identifier = row.id
        session.execute(
            update(Order)
            .where(Order.id == identifier)
            .values(
                id=row.id,
                number=row.number,
                consent_id=row.consent_id,
                idempotency_key=row.idempotency_key,
                status="in_progress",
                version=2,
            )
        )
        session.execute(
            update(Order)
            .where(Order.id == identifier)
            .values(
                pii_erased_at=utc_now(),
                contact_name=None,
                email=None,
                request_hash=None,
                receipt_token_hash=None,
            )
        )
    with orders_database.unit_of_work() as session:
        order = session.get(Order, identifier)
        assert (
            order is not None and order.status == "in_progress" and order.version == 2
        )
        assert order.pii_erased_at is not None and order.receipt_token_hash is None
