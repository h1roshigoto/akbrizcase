"""PR #51: actual publication wait, legacy decisions and delayed witnesses."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from queue import Queue
from threading import Event
from time import monotonic
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, insert, select, text, update
from sqlalchemy.exc import IntegrityError
from test_cleanup import create, terminal, witness
from test_cleanup import isolated_cleanup as isolated_cleanup
from test_privacy import PASSWORD, body, packet
from test_privacy import client as client
from test_privacy import privacy_db as privacy_db

from app.core.db import Database
from app.core.db_types import utc_now
from app.core.privacy import digest
from app.core.staff_models import StaffUser
from app.core.staff_sessions import login_staff
from app.modules.catalog.models import CatalogItem
from app.modules.catalog.seed import seed_catalog
from app.modules.notify.models import EmailOutbox
from app.modules.notify.outbox import Claim, finish_failure
from app.modules.notify.retry_service import retry_failed_notification
from app.modules.orders.cleanup_service import (
    apply_command,
    eligibility,
    order_policy,
    prepare_entry,
)
from app.modules.orders.deletion_journal import (
    JournalUnavailable,
    command_for,
)
from app.modules.orders.deletion_models import DeletionEntry
from app.modules.orders.deletion_operator import prepare_deletion
from app.modules.orders.models import (
    Consent,
    ConsentText,
    Order,
    OrderItem,
    OrderStatusHistory,
)
from app.modules.orders.privacy_models import ConsentPublication
from app.modules.orders.privacy_operator import run_privacy
from app.modules.orders.privacy_service import PrivacyUnavailable
from app.modules.orders.security import OrderSecurity
from app.modules.orders.seed import DEMO_VERSION, demo_bundle
from app.modules.orders.service import create_order
from app.schemas.common import OrderHeaders
from app.schemas.orders import OrderCreate

pytestmark = pytest.mark.integration


def test_order_waits_for_new_publication_without_using_transaction_time(
    privacy_db: tuple[Database, Database],
    tmp_path: Path,
) -> None:
    runtime, migrator = privacy_db
    path, bundle = packet(tmp_path)
    started: Queue[tuple[int, datetime]] = Queue()
    proceed = Event()

    def intake() -> int:
        with runtime.unit_of_work() as session:
            pid, transaction_at = session.execute(
                text("SELECT pg_backend_pid(), now()")
            ).one()
            started.put((pid, transaction_at))
            assert proceed.wait(5)
            return create_order(
                session,
                OrderCreate.model_validate(body(bundle.consent.version)),
                OrderHeaders.model_validate(
                    {"Idempotency-Key": str(uuid4()), "X-Request-Token": "d" * 64}
                ),
                OrderSecurity(b"synthetic-concurrent-privacy-key" * 2),
            ).number

    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(intake)
        waiter, transaction_at = started.get(timeout=5)
        try:
            with migrator.unit_of_work() as session:
                writer = session.scalar(text("SELECT pg_backend_pid()"))
                result = run_privacy(
                    session,
                    login="privacy-admin",
                    password=PASSWORD,
                    operation="publish",
                    expected_version=DEMO_VERSION,
                    path=path,
                    expected_checksum=bundle.checksum(),
                )
                assert result.ok
                publication = session.get(ConsentPublication, bundle.consent.version)
                assert (
                    publication is not None
                    and transaction_at < publication.published_at
                )
                proceed.set()
                deadline = monotonic() + 3
                with runtime.unit_of_work() as observer:
                    while not observer.scalar(
                        text("SELECT :writer=ANY(pg_blocking_pids(:waiter))"),
                        {"writer": writer, "waiter": waiter},
                    ):
                        assert monotonic() < deadline and not pending.done()
                        Event().wait(0.01)
            number = pending.result(timeout=5)
        finally:
            proceed.set()
    with runtime.unit_of_work() as session:
        row = session.scalars(select(Order).where(Order.number == number)).one()
        consent = session.get(Consent, row.consent_id)
        publication = session.get(ConsentPublication, bundle.consent.version)
        assert consent is not None and publication is not None
        assert (
            row.created_at
            == transaction_at
            < publication.published_at
            <= consent.granted_at
        )
        assert order_policy(session, row, utc_now()) == bundle
        row.status = "closed"
        row.terminal_at = row.closed_at = consent.granted_at + timedelta(seconds=1)
        now = row.terminal_at + timedelta(
            days=bundle.retention.order_pii_after_terminal_days
        )
        outbox = session.scalars(
            select(EmailOutbox).where(EmailOutbox.order_id == row.id)
        ).one()
        outbox.status, outbox.sent_at = "sent", row.terminal_at
        assert eligibility(session, "order_pii", row, now)[1]
        assert eligibility(session, "outbox_delete", outbox, now)[1]
        pii = command_for(prepare_entry(session, "order_pii", row.id, bundle, now))
        notification = command_for(
            prepare_entry(session, "outbox_delete", outbox.id, bundle, now)
        )
    for command in (pii, notification):
        proof = witness(tmp_path, command)
        with runtime.unit_of_work() as session:
            assert apply_command(session, command, proof, now=now)


def legacy_order(
    database: Database,
    *,
    kind: str = "support",
    text_version: str | None = None,
    granted_at: datetime | None = None,
) -> tuple[UUID, UUID]:
    seed_catalog(database, mode="test")
    with database.unit_of_work() as session:
        created = granted_at or utc_now() - timedelta(days=200)
        if text_version is None:
            text_version = "SYNTHETIC-LEGACY-" + uuid4().hex
            session.add(
                ConsentText(
                    version=text_version,
                    purpose="SYNTHETIC ONLY",
                    text="Synthetic historical consent",
                    checksum=digest("Synthetic historical consent"),
                    active_from=created - timedelta(days=1),
                )
            )
            session.flush()
        consent = Consent(text_version=text_version, granted_at=created)
        session.add(consent)
        session.flush()
        row = Order(
            kind=kind,
            consent_id=consent.id,
            idempotency_key=uuid4(),
            contact_name="Synthetic legacy",
            email="legacy@example.invalid",
            comment="Synthetic historical issue",
            request_hash="synthetic",
            receipt_token_hash="synthetic",
            known_subtotal=0,
            items_total=0 if kind == "commercial" else None,
            total_amount=0 if kind == "commercial" else None,
            delivery_status="not_required",
            delivery_cost=0,
            status="cancelled",
            created_at=created,
            terminal_at=created + timedelta(days=1),
        )
        session.add(row)
        session.flush()
        session.add_all(
            [
                OrderStatusHistory(order_id=row.id, to_status="new"),
                EmailOutbox(order_id=row.id),
            ]
        )
        if kind == "commercial":
            item = session.scalars(
                select(CatalogItem).where(CatalogItem.sku == "DEMO-P-003")
            ).one()
            session.add(
                OrderItem(
                    order_id=row.id,
                    item_id=item.id,
                    qty=1,
                    sku_snapshot=item.sku,
                    name_snapshot=item.name,
                    kind_snapshot="product",
                    unit_snapshot=item.unit,
                    price_mode_snapshot="fixed",
                    unit_price_snapshot=0,
                    currency_snapshot="RUB",
                    tax_note_snapshot="Synthetic",
                    requires_shipping_snapshot=False,
                    catalog_revision_snapshot=item.revision,
                )
            )
        return row.id, consent.id


@pytest.mark.parametrize("kind", ["support", "commercial"])
def test_legacy_manual_decision_separate_from_collection_policy_rollback_repeat(
    privacy_db: tuple[Database, Database],
    tmp_path: Path,
    kind: str,
) -> None:
    runtime, migrator = privacy_db
    identifier, consent_id = legacy_order(runtime, kind=kind)
    with runtime.unit_of_work() as session:
        row = session.get(Order, identifier)
        assert row is not None
        consent = session.get(Consent, consent_id)
        assert consent is not None
        original = consent.text_version
        assert session.get(ConsentPublication, original) is None
        with pytest.raises(PrivacyUnavailable):
            order_policy(session, row, utc_now())
    with migrator.unit_of_work() as session:
        code, command = prepare_deletion(
            session,
            login="privacy-admin",
            password=PASSWORD,
            order_id=identifier,
            expected_version=1,
            decision_id=uuid4(),
            journal_policy_version=DEMO_VERSION,
        )
        assert code == "PREPARED" and command is not None
        assert command.format == 2 and command.basis == "manual_decision"
        assert (
            command.policy_version is None and command.journal_version == DEMO_VERSION
        )
        assert session.get(Consent, consent_id).text_version == original  # type: ignore[union-attr]
        assert session.get(ConsentPublication, original) is None
    with migrator.unit_of_work() as session, pytest.raises(JournalUnavailable):
        apply_command(session, command, b"", manual_authorized=True)
    proof = witness(tmp_path, command)
    with pytest.raises(RuntimeError):
        with migrator.unit_of_work() as session:
            assert apply_command(session, command, proof, manual_authorized=True)
            assert session.get(Consent, consent_id) is None
            raise RuntimeError("Synthetic rollback after complete graph deletion")
    with migrator.unit_of_work() as session:
        assert session.get(Order, identifier) is not None
        assert session.get(Consent, consent_id) is not None
        assert session.scalar(select(func.count()).select_from(OrderStatusHistory)) == 1
        assert session.scalar(select(func.count()).select_from(EmailOutbox)) == 1
        assert session.scalar(select(func.count()).select_from(OrderItem)) == (
            1 if kind == "commercial" else 0
        )
        entry = session.get(DeletionEntry, command.id)
        assert entry is not None and entry.applied_at is None
        assert apply_command(session, command, proof, manual_authorized=True)
    with migrator.unit_of_work() as session:
        assert (
            session.get(Order, identifier) is None
            and session.get(Consent, consent_id) is None
        )
        assert not apply_command(session, command, proof, manual_authorized=True)
        assert session.get(ConsentText, original) is not None
        assert session.get(ConsentPublication, DEMO_VERSION) is not None


@pytest.mark.parametrize("actor", ["manager", "inactive", "wrong_password", "runtime"])
def test_legacy_manual_authority_denials(
    privacy_db: tuple[Database, Database], actor: str
) -> None:
    runtime, migrator = privacy_db
    identifier, _ = legacy_order(runtime)
    if actor == "inactive":
        with migrator.unit_of_work() as session:
            session.execute(
                update(StaffUser)
                .where(StaffUser.login == "privacy-admin")
                .values(is_active=False)
            )
    with (runtime if actor == "runtime" else migrator).unit_of_work() as session:
        code, command = prepare_deletion(
            session,
            login="privacy-manager" if actor == "manager" else "privacy-admin",
            password="synthetic-wrong" if actor == "wrong_password" else PASSWORD,
            order_id=identifier,
            expected_version=1,
            decision_id=uuid4(),
            journal_policy_version=DEMO_VERSION,
        )
        assert (
            code
            == {
                "manager": "FORBIDDEN",
                "inactive": "AUTHENTICATION_DENIED",
                "wrong_password": "AUTHENTICATION_DENIED",
                "runtime": "PROTECTED_DATABASE_ROLE_REQUIRED",
            }[actor]
        )
        assert command is None and session.get(Order, identifier) is not None
        assert session.scalar(select(func.count()).select_from(DeletionEntry)) == 0


def test_legacy_manual_requires_explicit_valid_journal_packet(
    privacy_db: tuple[Database, Database],
) -> None:
    runtime, migrator = privacy_db
    identifier, _ = legacy_order(runtime)
    with pytest.raises(PrivacyUnavailable):
        with migrator.unit_of_work() as session:
            prepare_deletion(
                session,
                login="privacy-admin",
                password=PASSWORD,
                order_id=identifier,
                expected_version=1,
                decision_id=uuid4(),
                journal_policy_version="missing-journal-approval",
            )
    with runtime.unit_of_work() as session:
        assert session.get(Order, identifier) is not None
        assert session.scalar(select(func.count()).select_from(DeletionEntry)) == 0


@pytest.mark.parametrize(
    "shape",
    [
        "auto_without_policy",
        "manual_with_collection_policy",
        "missing_journal_policy",
        "unknown_format",
        "invalid_journal_fk",
    ],
)
def test_database_enforces_new_basis_without_relaxing_existing_evidence(
    privacy_db: tuple[Database, Database],
    shape: str,
) -> None:
    at = utc_now()
    values: dict[str, object] = dict(
        id=uuid4(),
        format=2,
        operation="order_pii",
        object_id=uuid4(),
        consent_id=None,
        decision_id=None,
        policy_version=DEMO_VERSION,
        journal_policy_version=DEMO_VERSION,
        created_at=at,
        retain_until=at + timedelta(days=14),
    )
    if shape == "auto_without_policy":
        values["policy_version"] = None
    elif shape == "manual_with_collection_policy":
        values.update(operation="order_delete", consent_id=uuid4(), decision_id=uuid4())
    elif shape == "missing_journal_policy":
        values["journal_policy_version"] = None
    elif shape == "unknown_format":
        values["format"] = 3
    else:
        values.update(
            operation="order_delete",
            consent_id=uuid4(),
            decision_id=uuid4(),
            policy_version=None,
            journal_policy_version="missing-journal-approval",
        )
    with pytest.raises(IntegrityError):
        with privacy_db[1].unit_of_work() as session:
            session.execute(insert(DeletionEntry).values(**values))


@pytest.mark.parametrize("damage", ["after_grant", "checksum", "version"])
def test_automatic_policy_still_rejects_wrong_chronology_or_packet(
    privacy_db: tuple[Database, Database],
    tmp_path: Path,
    damage: str,
) -> None:
    runtime, migrator = privacy_db
    if damage == "after_grant":
        with runtime.unit_of_work() as session:
            at = session.scalars(
                select(ConsentPublication.published_at).where(
                    ConsentPublication.consent_version == DEMO_VERSION
                )
            ).one() - timedelta(seconds=1)
        identifier, _ = legacy_order(runtime, text_version=DEMO_VERSION, granted_at=at)
    else:
        _, bundle = packet(tmp_path)
        version = bundle.consent.version
        with migrator.unit_of_work() as session:
            consent = (
                bundle.consent.model_copy(update={"version": version + "-different"})
                if damage == "version"
                else bundle.consent
            )
            session.add(
                ConsentText(
                    **consent.model_dump(), active_from=utc_now() - timedelta(seconds=1)
                )
            )
            session.flush()
            session.add(
                ConsentPublication(
                    consent_version=consent.version,
                    bundle=bundle.model_dump(mode="json"),
                    bundle_checksum="0" * 64
                    if damage == "checksum"
                    else bundle.checksum(),
                    published_at=utc_now(),
                )
            )
        identifier, _ = legacy_order(
            runtime, text_version=consent.version, granted_at=utc_now()
        )
    with runtime.unit_of_work() as session:
        row = session.get(Order, identifier)
        assert row is not None
        with pytest.raises(PrivacyUnavailable):
            order_policy(session, row, utc_now())
        outbox = session.scalars(
            select(EmailOutbox).where(EmailOutbox.order_id == identifier)
        ).one()
        with pytest.raises(PrivacyUnavailable):
            eligibility(session, "outbox_delete", outbox, utc_now())


def test_retry_new_failure_does_not_expire_old_pending_witness(
    privacy_db: tuple[Database, Database],
    client: TestClient,
    tmp_path: Path,
) -> None:
    runtime, _ = privacy_db
    identifier = create(client, runtime)
    first = terminal(runtime, identifier)
    bundle = demo_bundle()
    prepared = first + timedelta(days=bundle.retention.outbox_failed_days)
    with runtime.unit_of_work() as session:
        row = session.scalars(
            select(EmailOutbox).where(EmailOutbox.order_id == identifier)
        ).one()
        row.status, row.failed_at, row.attempts = "failed", first, 5
        prepared_entry = prepare_entry(
            session, "outbox_delete", row.id, bundle, prepared
        )
        command = command_for(prepared_entry)
        row_id = row.id
    proof = witness(tmp_path, command)
    with runtime.unit_of_work() as session:
        from unittest.mock import patch

        issued = login_staff(session, "privacy-admin", PASSWORD)
        assert issued is not None
        with patch(
            "app.modules.notify.retry_service.database_now",
            return_value=prepared + timedelta(seconds=1),
        ):
            retry_failed_notification(session, identifier, issued.identity)
    second = command.retain_until + timedelta(days=2)
    with runtime.unit_of_work() as session:
        retry_row = session.get(EmailOutbox, row_id)
        assert retry_row is not None
        (
            retry_row.status,
            retry_row.attempts,
            retry_row.lease_token,
            retry_row.lease_until,
        ) = (
            "sending",
            5,
            uuid4(),
            second,
        )
        claim = Claim(retry_row.id, retry_row.order_id, 1, retry_row.lease_token, 5)
    with runtime.unit_of_work() as session:
        # Actual final-failure transition, with deterministic PostgreSQL time.
        from unittest.mock import patch

        with patch("app.modules.notify.outbox.database_now", return_value=second):
            assert finish_failure(session, claim, "SMTP_TIMEOUT")
    deadline = second + timedelta(days=bundle.retention.outbox_failed_days)
    with runtime.unit_of_work() as session:
        assert not apply_command(
            session, command, proof, now=deadline - timedelta(seconds=1)
        )
        assert session.get(EmailOutbox, row_id) is not None
        entry = session.get(DeletionEntry, command.id)
        assert entry is not None and entry.applied_at is None
        assert command_for(entry).canonical() == proof
        assert command.earliest_prune_at(None) is None
        assert apply_command(session, command, proof, now=deadline)
    with runtime.unit_of_work() as session:
        entry = session.get(DeletionEntry, command.id)
        assert entry is not None and entry.applied_at == deadline
        earliest = command.earliest_prune_at(entry.applied_at)
        assert earliest is not None
        assert earliest >= deadline + timedelta(
            days=max(14, bundle.retention.backup_days)
        )
        assert command.earliest_prune_at(entry.applied_at) == deadline + timedelta(
            days=bundle.retention.deletion_journal_days
        )
