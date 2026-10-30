"""PostgreSQL 16: approval gates, atomic publication and real lock contention."""

import json
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from threading import Event
from typing import Literal
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import IntegrityError, ProgrammingError

from app.core.db import Database
from app.core.db_types import utc_now
from app.core.privacy import PrivacyBundle, digest
from app.core.staff import bootstrap_admin, create_staff
from app.main import app
from app.modules.chat.models import ChatSession
from app.modules.notify.models import EmailOutbox
from app.modules.orders.models import Consent, ConsentText, Order
from app.modules.orders.privacy_models import ConsentPublication
from app.modules.orders.privacy_operator import PrivacyOutcome, run_privacy
from app.modules.orders.security import OrderSecurity
from app.modules.orders.seed import DEMO_VERSION, demo_bundle, seed_consent
from app.modules.orders.service import effective_consent

pytestmark = pytest.mark.integration
PASSWORD = "synthetic-privacy-test-password"


def body(version: str = DEMO_VERSION) -> dict[str, object]:
    return {
        "kind": "support",
        "items": [],
        "source": "site",
        "confirmed": True,
        "contact": {"name": "Synthetic test", "email": "test@example.invalid"},
        "comment": "Synthetic independent support description",
        "consent": {"version": version, "accepted": True},
    }


def headers() -> dict[str, str]:
    return {"Idempotency-Key": str(uuid4()), "X-Request-Token": "d" * 64}


@pytest.fixture
def privacy_db(
    databases: tuple[Database, Database], staff_database: Database
) -> Iterator[tuple[Database, Database]]:
    runtime, migrator = databases
    with migrator.unit_of_work() as session:
        session.execute(update(ConsentText).values(retired_at=utc_now()))
        # This fixture runs only in conftest's fresh owner-marked disposable DB.
        # Isolate immutable publication history so the newest packet belongs to
        # this test; runtime UPDATE/DELETE protection remains enabled and tested.
        session.execute(text("TRUNCATE app.consent_publications"))
    seed_consent(runtime, mode="test")
    with migrator.unit_of_work() as session:
        session.execute(
            update(ConsentText)
            .where(ConsentText.version == DEMO_VERSION)
            .values(retired_at=None)
        )
        assert bootstrap_admin(session, login="privacy-admin", password=PASSWORD).ok
        assert create_staff(
            session,
            admin_login="privacy-admin",
            admin_password=PASSWORD,
            login="privacy-manager",
            password=PASSWORD,
            role="manager",
        ).ok
    try:
        yield runtime, migrator
    finally:
        with migrator.unit_of_work() as session:
            session.execute(delete(Order))
            session.execute(delete(ChatSession))
            session.execute(delete(Consent))
            session.execute(update(ConsentText).values(retired_at=utc_now()))
            # Restore fixture isolation for the subsequent MVP-036 demo setup,
            # which seeds its own sole publication. This is never runtime DML.
            session.execute(text("TRUNCATE app.consent_publications"))


@pytest.fixture
def client(privacy_db: tuple[Database, Database]) -> Iterator[TestClient]:
    with TestClient(app) as result:
        app.state.database = privacy_db[0]
        app.state.order_security = OrderSecurity(b"synthetic-privacy-key" * 3)
        yield result


def packet(tmp_path: Path, *, approved: bool = False) -> tuple[Path, PrivacyBundle]:
    data = demo_bundle().model_dump(mode="json")
    data["consent"]["version"] = "SYNTHETIC-PRIVACY-" + uuid4().hex
    if approved:
        data["approval_status"] = "APPROVED"
        data["approval_ref"] = "test-fixture-approval-reference"
        data["consent"] = {
            "version": "TEST-ONLY-" + uuid4().hex,
            "purpose": "TEST FIXTURE ONLY",
            "text": "TEST FIXTURE ONLY. No legal approval.",
            "checksum": digest("TEST FIXTURE ONLY. No legal approval."),
        }
        policy_text = "TEST FIXTURE ONLY. No legal approval or operator details."
        data["policy"] = {
            "version": "TEST-POLICY-" + uuid4().hex,
            "text": policy_text,
            "checksum": digest(policy_text),
            "withdrawal_instructions": "TEST ONLY. Use the agreed test channel.",
        }
    bundle = PrivacyBundle.model_validate(data)
    path = tmp_path / (bundle.consent.version + ".json")
    path.write_text(bundle.model_dump_json())
    return path, bundle


def publish(
    database: Database,
    path: Path,
    bundle: PrivacyBundle,
    expected: str | None = DEMO_VERSION,
    login: str = "privacy-admin",
) -> PrivacyOutcome:
    with database.unit_of_work() as session:
        return run_privacy(
            session,
            login=login,
            password=PASSWORD,
            operation="publish",
            expected_version=expected,
            path=path,
            expected_checksum=bundle.checksum(),
        )


def test_publish_stale_form_replay_policy_and_atomic_evidence(
    privacy_db: tuple[Database, Database],
    client: TestClient,
    tmp_path: Path,
) -> None:
    original, key = body(), headers()
    first = client.post("/api/v1/orders", json=original, headers=key)
    assert first.status_code == 201
    path, bundle = packet(tmp_path)
    assert publish(privacy_db[1], path, bundle).ok
    assert client.get("/api/v1/consent-text").json() == bundle.consent.model_dump()
    policy = client.get("/api/v1/privacy-policy").json()
    assert policy["text"] == bundle.policy.text and policy["synthetic"] is True
    assert policy["consent_version"] == bundle.consent.version
    stale = client.post("/api/v1/orders", json=original, headers=headers())
    assert (
        stale.status_code == 409 and stale.json()["error"]["code"] == "CONSENT_CHANGED"
    )
    assert (
        client.post("/api/v1/orders", json=original, headers=key).json() == first.json()
    )
    assert (
        client.post(
            "/api/v1/orders", json=body(bundle.consent.version), headers=headers()
        ).status_code
        == 201
    )
    with privacy_db[0].unit_of_work() as session:
        assert session.scalar(select(func.count()).select_from(Order)) == 2
        assert session.scalar(select(func.count()).select_from(Consent)) == 2
        assert session.scalar(select(func.count()).select_from(EmailOutbox)) == 2
        assert (
            session.scalar(
                select(ConsentText.retired_at).where(
                    ConsentText.version == DEMO_VERSION
                )
            )
            is not None
        )
    # Neither publicly writable documents nor public deletion are introduced.
    for endpoint in (
        "/api/v1/consent-text",
        "/api/v1/privacy-policy",
        "/api/v1/orders/delete",
    ):
        assert client.post(endpoint, json={}).status_code in {404, 405}


def test_production_missing_config_synthetic_and_mismatched_packet(
    privacy_db: tuple[Database, Database],
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path, bundle = packet(tmp_path, approved=True)
    assert publish(privacy_db[1], path, bundle).ok
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("PRIVACY_CONFIG_FILE", raising=False)
    assert client.get("/api/v1/consent-text").status_code == 503
    policy = client.get("/api/v1/privacy-policy")
    assert policy.status_code == 200
    assert policy.json()["text"] == bundle.policy.text
    assert policy.json()["collection_enabled"] is False
    assert (
        client.post(
            "/api/v1/orders", json=body(bundle.consent.version), headers=headers()
        ).status_code
        == 503
    )
    assert (
        client.post("/api/v1/chat/sessions", json={"mode": "diagnose"}).status_code
        == 503
    )
    monkeypatch.setenv("PRIVACY_CONFIG_FILE", str(path))
    assert client.get("/api/v1/privacy-policy").json()["synthetic"] is False
    accepted_key = headers()
    accepted = client.post(
        "/api/v1/orders", json=body(bundle.consent.version), headers=accepted_key
    )
    assert accepted.status_code == 201
    path.write_text(demo_bundle().model_dump_json())
    replay = client.post(
        "/api/v1/orders", json=body(bundle.consent.version), headers=accepted_key
    )
    assert replay.status_code == 200 and replay.json() == accepted.json()
    readonly = client.get("/api/v1/privacy-policy")
    assert readonly.status_code == 200
    assert readonly.json()["collection_enabled"] is False
    assert (
        client.post(
            "/api/v1/orders", json=body(bundle.consent.version), headers=headers()
        ).status_code
        == 503
    )


@pytest.mark.parametrize("role", ["privacy-manager", "missing-user", "privacy-admin"])
def test_no_insufficient_staff_or_runtime_role_bypass(
    privacy_db: tuple[Database, Database],
    tmp_path: Path,
    role: str,
) -> None:
    path, bundle = packet(tmp_path)
    database = privacy_db[0] if role == "privacy-admin" else privacy_db[1]
    outcome = publish(database, path, bundle, login=role)
    assert (
        outcome.code
        == {
            "privacy-admin": "PROTECTED_DATABASE_ROLE_REQUIRED",
            "privacy-manager": "FORBIDDEN",
            "missing-user": "AUTHENTICATION_DENIED",
        }[role]
    )
    with privacy_db[0].unit_of_work() as session:
        assert session.get(ConsentText, bundle.consent.version) is None
        assert effective_consent(session).version == DEMO_VERSION


def test_invalid_approval_and_late_failure_roll_back_everything(
    privacy_db: tuple[Database, Database],
    tmp_path: Path,
) -> None:
    runtime, migrator = privacy_db
    path, bundle = packet(tmp_path)
    with migrator.unit_of_work() as session:
        result = run_privacy(
            session,
            login="privacy-admin",
            password=PASSWORD,
            operation="publish",
            expected_version=DEMO_VERSION,
            path=path,
            expected_checksum="0" * 64,
        )
        assert result.code == "INVALID_APPROVAL_PACKET"
    with pytest.raises(RuntimeError, match="late failure"):
        with migrator.unit_of_work() as session:
            assert run_privacy(
                session,
                login="privacy-admin",
                password=PASSWORD,
                operation="publish",
                expected_version=DEMO_VERSION,
                path=path,
                expected_checksum=bundle.checksum(),
            ).ok
            raise RuntimeError("late failure")
    with runtime.unit_of_work() as session:
        assert effective_consent(session).version == DEMO_VERSION
        assert session.get(ConsentText, bundle.consent.version) is None
        assert session.get(ConsentPublication, bundle.consent.version) is None
    assert publish(migrator, path, bundle).ok


def test_retirement_no_reactivation_and_permanent_packet(
    privacy_db: tuple[Database, Database],
    client: TestClient,
    tmp_path: Path,
) -> None:
    runtime, migrator = privacy_db
    path, bundle = packet(tmp_path)
    assert publish(migrator, path, bundle).ok
    with migrator.unit_of_work() as session:
        assert run_privacy(
            session,
            login="privacy-admin",
            password=PASSWORD,
            operation="retire",
            expected_version=bundle.consent.version,
        ).ok
    assert client.get("/api/v1/consent-text").status_code == 503
    policy = client.get("/api/v1/privacy-policy")
    assert policy.status_code == 200
    assert policy.json()["text"] == bundle.policy.text
    assert (
        policy.json()["withdrawal_instructions"]
        == bundle.policy.withdrawal_instructions
    )
    assert policy.json()["collection_enabled"] is False
    assert (
        client.post("/api/v1/orders", json=body(), headers=headers()).status_code == 503
    )
    assert publish(migrator, path, bundle, expected=None).code == "VERSION_EXISTS"
    for database in (runtime, migrator):
        for statement in (
            "UPDATE app.consent_publications SET bundle='{}'",
            "DELETE FROM app.consent_publications",
        ):
            with pytest.raises((IntegrityError, ProgrammingError)):
                with database.unit_of_work() as session:
                    session.execute(text(statement))


def test_future_publication_and_conflict_fail_closed(
    privacy_db: tuple[Database, Database],
    tmp_path: Path,
) -> None:
    runtime, migrator = privacy_db
    path, bundle = packet(tmp_path)
    data = bundle.model_dump(mode="json") | {
        "approved_at": (utc_now() + timedelta(days=1)).isoformat()
    }
    path.write_text(json.dumps(data))
    assert publish(migrator, path, bundle).code == "INVALID_APPROVAL_PACKET"
    with migrator.unit_of_work() as session:
        session.add(
            ConsentText(
                **bundle.consent.model_dump(), active_from=utc_now() + timedelta(days=1)
            )
        )
    path, next_bundle = packet(tmp_path)
    assert publish(migrator, path, next_bundle).code == "STATE_CONFLICT"
    # Safe refusal on ambiguous effective versions is additionally covered by
    # the existing order-creation integration matrix (including wrong checksum).
    with runtime.unit_of_work() as session:
        assert effective_consent(session).version == DEMO_VERSION


def test_concurrent_publishers_compare_expected_version(
    privacy_db: tuple[Database, Database],
    tmp_path: Path,
) -> None:
    packets = [packet(tmp_path), packet(tmp_path)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(publish, privacy_db[1], path, bundle)
            for path, bundle in packets
        ]
        assert sorted(f.result(timeout=10).code for f in futures) == [
            "STATE_CONFLICT",
            "SUCCESS",
        ]
    with privacy_db[0].unit_of_work() as session:
        assert effective_consent(session).version in {
            bundle.consent.version for _, bundle in packets
        }


def test_publication_waits_for_order_transaction(
    privacy_db: tuple[Database, Database],
    tmp_path: Path,
) -> None:
    runtime, migrator = privacy_db
    path, bundle = packet(tmp_path)
    started = Event()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with runtime.unit_of_work() as session:
            assert effective_consent(session, lock=True).version == DEMO_VERSION

            def writer() -> PrivacyOutcome:
                started.set()
                return publish(migrator, path, bundle)

            future = pool.submit(writer)
            assert started.wait(2)
            # An actual PG advisory waiter proves publication cannot pass the gate.
            from time import monotonic

            deadline = monotonic() + 2
            waiting = False
            while monotonic() < deadline:
                with runtime.engine.connect() as connection:
                    waiting = bool(
                        connection.scalar(
                            text(
                                "SELECT EXISTS(SELECT 1 FROM pg_locks "
                                "WHERE locktype='advisory' AND NOT granted)"
                            )
                        )
                    )
                if waiting:
                    break
            assert waiting and not future.done()
        assert future.result(timeout=10).ok


@pytest.mark.parametrize("operation", ["publish", "retire"])
def test_order_creation_waits_for_publication_and_requires_fresh_confirmation(
    privacy_db: tuple[Database, Database],
    client: TestClient,
    tmp_path: Path,
    operation: Literal["publish", "retire"],
) -> None:
    path, bundle = packet(tmp_path)
    started = Event()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with privacy_db[1].unit_of_work() as session:
            assert run_privacy(
                session,
                login="privacy-admin",
                password=PASSWORD,
                operation=operation,
                expected_version=DEMO_VERSION,
                path=path,
                expected_checksum=bundle.checksum(),
            ).ok

            def submit() -> int:
                started.set()
                return client.post(
                    "/api/v1/orders", json=body(), headers=headers()
                ).status_code

            future = pool.submit(submit)
            assert started.wait(2)
            from time import monotonic

            deadline = monotonic() + 2
            waiting = False
            while monotonic() < deadline:
                with privacy_db[0].engine.connect() as connection:
                    waiting = bool(
                        connection.scalar(
                            text(
                                "SELECT EXISTS(SELECT 1 FROM pg_locks "
                                "WHERE locktype='advisory' AND NOT granted)"
                            )
                        )
                    )
                if waiting:
                    break
            assert waiting and not future.done()
        assert future.result(timeout=10) == (409 if operation == "publish" else 503)
    if operation == "retire":
        assert publish(privacy_db[1], path, bundle, expected=None).ok
    assert (
        client.post(
            "/api/v1/orders", json=body(bundle.consent.version), headers=headers()
        ).status_code
        == 201
    )
    with privacy_db[0].unit_of_work() as session:
        accepted = session.scalars(select(Consent)).one()
        published = session.get(ConsentText, bundle.consent.version)
        assert published is not None and accepted.granted_at >= published.active_from


def test_production_document_privileges_allow_locks_but_deny_mutations(
    privacy_db: tuple[Database, Database],
    client: TestClient,
) -> None:
    runtime, migrator = privacy_db
    # Apply exactly the production grants on this isolated, marked test DB.
    with migrator.unit_of_work() as session:
        session.execute(
            text(
                "REVOKE INSERT, UPDATE, DELETE ON app.consent_texts "
                "FROM akbrizcase_runtime"
            )
        )
        session.execute(
            text(
                "REVOKE INSERT, UPDATE, DELETE ON app.consent_publications "
                "FROM akbrizcase_runtime"
            )
        )
        session.execute(
            text("GRANT UPDATE(version) ON app.consent_texts TO akbrizcase_runtime")
        )
    try:
        assert (
            client.post("/api/v1/orders", json=body(), headers=headers()).status_code
            == 201
        )
        for statement in (
            "UPDATE app.consent_texts SET retired_at=clock_timestamp()",
            "DELETE FROM app.consent_texts",
            "INSERT INTO app.consent_texts SELECT * FROM app.consent_texts",
            "INSERT INTO app.consent_publications "
            "SELECT * FROM app.consent_publications",
        ):
            with pytest.raises(ProgrammingError):
                with runtime.unit_of_work() as session:
                    session.execute(text(statement))
        # The fixed demo seed remains available after owner marking; it cannot
        # grant broad write access or overwrite existing version/evidence.
        seed_consent(runtime, mode="test")
        with runtime.unit_of_work() as session:
            assert effective_consent(session, lock=True).version == DEMO_VERSION
    finally:
        with migrator.unit_of_work() as session:
            session.execute(
                text(
                    "GRANT INSERT, UPDATE, DELETE ON app.consent_texts "
                    "TO akbrizcase_runtime"
                )
            )
            session.execute(
                text("GRANT INSERT ON app.consent_publications TO akbrizcase_runtime")
            )


@pytest.mark.parametrize("blocked_by", ["retire", "missing-config"])
def test_committed_chat_replay_survives_collection_shutdown(
    privacy_db: tuple[Database, Database],
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    blocked_by: str,
) -> None:
    from app.modules.chat.mode_router import ModeRouter
    from app.modules.chat.models import ChatMessage

    path, bundle = packet(tmp_path, approved=True)
    assert publish(privacy_db[1], path, bundle).ok
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("PRIVACY_CONFIG_FILE", str(path))
    own = client.post("/api/v1/chat/sessions", json={"mode": "knowledge"}).json()
    token = {"X-Chat-Token": own["session_token"]}
    original = {
        "session_id": own["session_id"],
        "request_id": str(uuid4()),
        "expected_version": 0,
        "input": {"type": "select_mode", "mode": "commerce"},
    }
    first = client.post("/api/v1/chat/messages", json=original, headers=token)
    assert first.status_code == 200
    with privacy_db[0].unit_of_work() as session:
        row = session.get(ChatSession, own["session_id"])
        assert row is not None
        before = (row.state_version, row.last_activity_at, row.mode)
    if blocked_by == "retire":
        with privacy_db[1].unit_of_work() as session:
            assert run_privacy(
                session,
                login="privacy-admin",
                password=PASSWORD,
                operation="retire",
                expected_version=bundle.consent.version,
            ).ok
    else:
        monkeypatch.delenv("PRIVACY_CONFIG_FILE")
    monkeypatch.setattr(
        ModeRouter, "route", lambda *args: pytest.fail("Replay must not route")
    )
    # The original committed response was lost. Only its immutable identity is
    # reused; a fresh projector supplies the actual response after COMMIT.
    replay = client.post("/api/v1/chat/messages", json=original, headers=token)
    assert replay.status_code == 200 and replay.json() == first.json()
    changed = original | {"input": {"type": "select_mode", "mode": "knowledge"}}
    conflict = client.post("/api/v1/chat/messages", json=changed, headers=token)
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    fresh = original | {"request_id": str(uuid4()), "expected_version": 1}
    assert (
        client.post("/api/v1/chat/messages", json=fresh, headers=token).status_code
        == 503
    )
    wrong = client.post(
        "/api/v1/chat/messages", json=original, headers={"X-Chat-Token": "f" * 64}
    )
    assert wrong.status_code == 404
    policy = client.get("/api/v1/privacy-policy")
    assert policy.status_code == 200 and policy.json()["collection_enabled"] is False
    assert policy.json()["text"] == bundle.policy.text
    assert client.get("/api/v1/consent-text").status_code == 503
    assert (
        client.post(
            "/api/v1/orders", json=body(bundle.consent.version), headers=headers()
        ).status_code
        == 503
    )
    with privacy_db[0].unit_of_work() as session:
        row = session.get(ChatSession, own["session_id"])
        assert (
            row is not None
            and (row.state_version, row.last_activity_at, row.mode) == before
        )
        assert session.scalar(select(func.count()).select_from(ChatMessage)) == 1
        row.created_at = utc_now() - timedelta(hours=25)
        row.last_activity_at = row.created_at
        row.expires_at = row.created_at + timedelta(hours=24)
    assert (
        client.post("/api/v1/chat/messages", json=original, headers=token).status_code
        == 410
    )
    assert (
        client.post(
            "/api/v1/chat/messages", json=original, headers={"X-Chat-Token": "f" * 64}
        ).status_code
        == 404
    )


@pytest.mark.parametrize("operation", ["retire", "publish"])
def test_new_chat_waits_for_privacy_change_before_session_lock(
    privacy_db: tuple[Database, Database],
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: Literal["retire", "publish"],
) -> None:
    from test_catalog_applicability import wait_for_lock

    from app.modules.chat.models import ChatMessage

    path, bundle = packet(tmp_path, approved=True)
    assert publish(privacy_db[1], path, bundle).ok
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("PRIVACY_CONFIG_FILE", str(path))
    own = client.post("/api/v1/chat/sessions", json={"mode": "knowledge"}).json()
    next_path, next_bundle = packet(tmp_path, approved=True)
    request = {
        "session_id": own["session_id"],
        "request_id": str(uuid4()),
        "expected_version": 0,
        "input": {"type": "select_mode", "mode": "commerce"},
    }
    with ThreadPoolExecutor(max_workers=1) as pool:
        with privacy_db[1].unit_of_work() as session:
            assert run_privacy(
                session,
                login="privacy-admin",
                password=PASSWORD,
                operation=operation,
                expected_version=bundle.consent.version,
                path=next_path,
                expected_checksum=next_bundle.checksum(),
            ).ok
            future = pool.submit(
                client.post,
                "/api/v1/chat/messages",
                json=request,
                headers={"X-Chat-Token": own["session_token"]},
            )
            wait_for_lock(privacy_db[0])
            assert not future.done()
            # The contender waits for privacy, without holding the session row.
            session.execute(text("SET LOCAL lock_timeout='500ms'"))
            session.execute(
                select(ChatSession)
                .where(ChatSession.id == own["session_id"])
                .with_for_update()
            )
        assert future.result(timeout=10).status_code == 503
    with privacy_db[0].unit_of_work() as session:
        assert session.scalar(select(func.count()).select_from(ChatMessage)) == 0
        row = session.get(ChatSession, own["session_id"])
        assert row is not None and row.state_version == 0
    if operation == "publish":
        monkeypatch.setenv("PRIVACY_CONFIG_FILE", str(next_path))
        assert (
            client.post(
                "/api/v1/chat/messages",
                json=request,
                headers={"X-Chat-Token": own["session_token"]},
            ).status_code
            == 200
        )


@pytest.mark.parametrize(
    "damage",
    [
        "packet-checksum",
        "text-checksum",
        "approval-status",
        "future",
        "same-time",
        "overlap",
    ],
)
def test_policy_never_falls_back_from_newer_untrusted_publication(
    privacy_db: tuple[Database, Database],
    client: TestClient,
    tmp_path: Path,
    damage: str,
) -> None:
    path, good = packet(tmp_path)
    assert publish(privacy_db[1], path, good).ok
    assert client.get("/api/v1/privacy-policy").json()["version"] == good.policy.version
    _, newer = packet(tmp_path)
    with privacy_db[1].unit_of_work() as session:
        moment = utc_now()
        if damage != "overlap":
            session.execute(update(ConsentText).values(retired_at=moment))
        original = session.get(ConsentPublication, good.consent.version)
        assert original is not None
        at = original.published_at if damage == "same-time" else moment
        if damage == "future":
            at += timedelta(days=1)
        data = newer.model_dump(mode="json")
        if damage == "approval-status":
            data["approval_status"] = "PENDING EXTERNAL APPROVAL"
        session.add(
            ConsentText(
                **(
                    newer.consent.model_dump()
                    | ({"checksum": "0" * 64} if damage == "text-checksum" else {})
                ),
                active_from=at,
            )
        )
        session.flush()
        session.add(
            ConsentPublication(
                consent_version=newer.consent.version,
                bundle=data,
                bundle_checksum="0" * 64
                if damage == "packet-checksum"
                else newer.checksum(),
                published_at=at,
            )
        )
    assert client.get("/api/v1/privacy-policy").status_code == 503


def test_policy_requires_a_publication_and_rejects_synthetic_in_production(
    privacy_db: tuple[Database, Database],
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    assert client.get("/api/v1/privacy-policy").status_code == 503
    with privacy_db[1].unit_of_work() as session:
        session.execute(text("TRUNCATE app.consent_publications"))
    assert client.get("/api/v1/privacy-policy").status_code == 503


def test_newest_approved_policy_stays_current_without_runtime_config(
    privacy_db: tuple[Database, Database],
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_path, first = packet(tmp_path, approved=True)
    assert publish(privacy_db[1], first_path, first).ok
    next_path, newer = packet(tmp_path, approved=True)
    assert publish(privacy_db[1], next_path, newer, expected=first.consent.version).ok
    monkeypatch.setenv("APP_ENV", "production")
    # A stale deployment file must not make the old policy current again.
    monkeypatch.setenv("PRIVACY_CONFIG_FILE", str(first_path))
    current = client.get("/api/v1/privacy-policy")
    assert current.status_code == 200
    assert current.json()["version"] == newer.policy.version
    assert current.json()["consent_version"] == newer.consent.version
    assert current.json()["collection_enabled"] is False
    assert client.get("/api/v1/consent-text").status_code == 503
    monkeypatch.setenv("PRIVACY_CONFIG_FILE", str(next_path))
    assert client.get("/api/v1/privacy-policy").json()["collection_enabled"] is True


@pytest.mark.parametrize("operation", ["retire", "publish"])
def test_committed_new_message_holds_privacy_lock_until_commit(
    privacy_db: tuple[Database, Database],
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: Literal["retire", "publish"],
) -> None:
    from test_catalog_applicability import wait_for_lock

    from app.modules.chat.messages import create_message
    from app.schemas.chat import ChatMessageCreate

    path, bundle = packet(tmp_path, approved=True)
    assert publish(privacy_db[1], path, bundle).ok
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("PRIVACY_CONFIG_FILE", str(path))
    own = client.post("/api/v1/chat/sessions", json={"mode": "knowledge"}).json()
    next_path, next_bundle = packet(tmp_path, approved=True)
    original = {
        "session_id": own["session_id"],
        "request_id": str(uuid4()),
        "expected_version": 0,
        "input": {"type": "select_mode", "mode": "commerce"},
    }

    def change() -> PrivacyOutcome:
        with privacy_db[1].unit_of_work() as session:
            return run_privacy(
                session,
                login="privacy-admin",
                password=PASSWORD,
                operation=operation,
                expected_version=bundle.consent.version,
                path=next_path,
                expected_checksum=next_bundle.checksum(),
            )

    with ThreadPoolExecutor(max_workers=1) as pool:
        with privacy_db[0].unit_of_work() as session:
            message_id = create_message(
                session,
                ChatMessageCreate.model_validate(original),
                own["session_token"],
            )
            future = pool.submit(change)
            wait_for_lock(privacy_db[0])
            assert not future.done()
        assert future.result(timeout=10).ok
    replay = client.post(
        "/api/v1/chat/messages",
        json=original,
        headers={"X-Chat-Token": own["session_token"]},
    )
    assert replay.status_code == 200
    assert (
        replay.json()["message_id"] == str(message_id)
        and replay.json()["state_version"] == 1
    )
