"""Image acceptance with PostgreSQL 16, real ClamAV/FDs, HTTP and commit races."""

import io
import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from threading import Event
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pytest import MonkeyPatch
from sqlalchemy import event, select, text, update
from sqlalchemy.exc import SQLAlchemyError
from test_kb_import import SyntheticClamAV
from test_kb_lifecycle import live_http
from test_order_reads import business_rows, create, login
from test_order_reads import read_client as read_client

from app.core.csrf import SESSION_COOKIE
from app.core.db import Database
from app.core.db_types import utc_now
from app.core.staff_models import AuditEvent, StaffSession, StaffUser
from app.core.staff_sessions import StaffIdentity, resolve_session
from app.main import app
from app.modules.catalog.image_service import CatalogImageService
from app.modules.catalog.images import pipeline as pipeline_module
from app.modules.catalog.images.pipeline import ImagePipeline
from app.modules.catalog.models import CatalogItem
from app.modules.catalog.router import image_pipeline
from app.modules.catalog.write_dto import ItemState
from app.modules.catalog.write_service import CatalogRejected, CatalogWriteService
from app.modules.kb.files import FilePipeline, FileRejected
from app.modules.kb.files.process import Limits
from app.modules.orders.seed import DEMO_VERSION

pytestmark = pytest.mark.integration


def picture(fmt: str = "PNG", color: str = "red") -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (32, 16), color).save(buffer, format=fmt)
    return buffer.getvalue()


@pytest.fixture
def images(tmp_path: Path) -> ImagePipeline:
    signatures = tmp_path / "signatures"
    signatures.mkdir()
    (signatures / "test.ndb").write_text(
        "Synthetic.Image.Test:0:*:53594e5448494e464543544544\n"
    )
    root = Path(os.environ["APP_FILES_DIR"]) / ("image-" + uuid4().hex)
    root.mkdir(mode=0o700)
    return ImagePipeline(root, _scanner=SyntheticClamAV(signatures))


def actor(client: TestClient, database: Database, role: str = "admin") -> StaffIdentity:
    login(client, role)
    token = client.cookies.get(SESSION_COOKIE)
    assert token is not None
    with database.unit_of_work() as session:
        identity = resolve_session(session, token)
        assert identity is not None
        return identity


def item(database: Database, kind: str = "product") -> CatalogItem:
    with database.unit_of_work() as session:
        value = session.scalar(
            select(CatalogItem).where(CatalogItem.kind == kind).limit(1)
        )
        assert value is not None
        return value


def replace(
    database: Database,
    identity: StaffIdentity,
    images: ImagePipeline,
    row: CatalogItem,
    fmt: str = "PNG",
    color: str = "red",
) -> CatalogItem:
    with database.unit_of_work() as session:
        return CatalogImageService(session, identity, images).replace(
            row.id,
            row.revision,
            io.BytesIO(picture(fmt, color)),
            filename="a." + fmt.lower(),
            mime="image/jpeg" if fmt == "JPEG" else "image/png",
        )


def url(row: CatalogItem) -> str:
    return f"/api/v1/catalog/items/{row.id}/image/{row.image_key}"


@contextmanager
def serving(images: ImagePipeline) -> Iterator[None]:
    app.dependency_overrides[image_pipeline] = lambda: images
    try:
        yield
    finally:
        app.dependency_overrides.pop(image_pipeline, None)


@pytest.mark.parametrize("kind", ["product", "service"])
@pytest.mark.parametrize("fmt", ["JPEG", "PNG"])
def test_install_replace_remove_http_and_orders(
    read_client: TestClient,
    staff_database: Database,
    images: ImagePipeline,
    kind: str,
    fmt: str,
) -> None:
    identity = actor(read_client, staff_database)
    create(read_client, staff_database)
    before = business_rows(staff_database)
    row = item(staff_database, kind)
    initial_revision = row.revision
    first = replace(staff_database, identity, images, row, fmt)
    assert first.revision == initial_revision + 1
    assert first.updated_at > row.updated_at
    assert first.image_key is not None
    stale_order = read_client.post(
        "/api/v1/orders",
        json={
            "kind": "commercial",
            "items": [
                {
                    "item_id": str(row.id),
                    "qty": 1,
                    "expected_revision": initial_revision,
                }
            ],
            "contact": {"name": "SYNTHETIC", "email": "synthetic@example.invalid"},
            "source": "site",
            "confirmed": True,
            "consent": {"version": DEMO_VERSION, "accepted": True},
        },
        headers={"Idempotency-Key": str(uuid4()), "X-Request-Token": "d" * 64},
    )
    assert stale_order.status_code == 409
    assert stale_order.json()["error"]["code"] == "CATALOG_CHANGED"
    historical = images.root / "images/verified" / first.image_key
    first_bytes = historical.read_bytes()
    with serving(images):
        response = read_client.get(url(first))
        assert response.status_code == 200
        assert response.content == first_bytes
        assert response.headers["content-type"] == (
            "image/jpeg" if fmt == "JPEG" else "image/png"
        )
        assert response.headers["content-disposition"] == "inline"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["cache-control"] == "no-store"
        detail = read_client.get(f"/api/v1/catalog/items/{first.id}").json()
        page = read_client.get("/api/v1/catalog/items").json()
        assert detail["image_url"] == url(first)
        assert next(card for card in page["items"] if card["id"] == str(first.id))[
            "image_url"
        ] == url(first)
        assert (
            replace(staff_database, identity, images, first, fmt).revision
            == first.revision
        )
        second = replace(staff_database, identity, images, first, fmt, "blue")
        assert (
            second.revision == first.revision + 1
            and second.image_key != first.image_key
        )
        assert read_client.get(url(first)).status_code == 404
        assert read_client.get(url(second)).status_code == 200
        with pytest.raises(CatalogRejected) as conflict:
            replace(staff_database, identity, images, first, fmt, "green")
        assert conflict.value.status == 409
        with staff_database.unit_of_work() as session:
            removed = CatalogImageService(session, identity, images).remove(
                second.id, second.revision
            )
        assert removed.revision == second.revision + 1 and removed.image_key is None
        assert read_client.get(url(second)).status_code == 404
        assert (
            read_client.get(f"/api/v1/catalog/items/{first.id}").json()["image_url"]
            is None
        )
        with staff_database.unit_of_work() as session:
            repeated = CatalogImageService(session, identity, images).remove(
                removed.id, removed.revision
            )
        assert repeated.revision == removed.revision
    assert historical.read_bytes() == first_bytes
    assert before == business_rows(staff_database)
    with staff_database.unit_of_work() as session:
        audits = list(
            session.scalars(
                select(AuditEvent).where(
                    AuditEvent.entity_id == first.id,
                    AuditEvent.action == "catalog_item.edit",
                )
            )
        )
        assert len(audits) == 3
        assert all(
            audit.actor_id == identity.user.id and audit.result == "success"
            for audit in audits
        )


@pytest.mark.parametrize("status", ["draft", "archived"])
def test_nonpublic_missing_and_invalid_http(
    read_client: TestClient,
    staff_database: Database,
    images: ImagePipeline,
    status: str,
) -> None:
    identity = actor(read_client, staff_database)
    row = replace(staff_database, identity, images, item(staff_database))
    with staff_database.unit_of_work() as session:
        session.execute(
            update(CatalogItem).where(CatalogItem.id == row.id).values(status=status)
        )
    with serving(images):
        for target in (
            url(row),
            url(row).replace(str(row.id), str(uuid4())),
            f"/api/v1/catalog/items/{row.id}/image/invalid",
            f"/api/v1/catalog/items/{row.id}/image/%2e%2e%2fetc%2fpasswd",
        ):
            response = read_client.get(target)
            assert response.status_code == 404
            assert (
                "checksum" not in response.text
                and str(images.root) not in response.text
            )


def snapshot(database: Database) -> tuple[object, ...]:
    with database.unit_of_work() as session:
        return tuple(
            session.execute(text(sql)).all()
            for sql in (
                "SELECT id,image_key,revision,updated_at "
                "FROM app.catalog_items ORDER BY id",
                "SELECT * FROM app.audit_events ORDER BY id",
            )
        )


@pytest.mark.parametrize(
    "fault",
    [
        "first_scan",
        "second_scan",
        "decode",
        "write",
        "final_integrity",
        "audit",
        "commit",
    ],
)
def test_failures_preserve_current_image_and_audit(
    read_client: TestClient,
    staff_database: Database,
    images: ImagePipeline,
    fault: str,
    monkeypatch: MonkeyPatch,
) -> None:
    identity = actor(read_client, staff_database)
    row = replace(staff_database, identity, images, item(staff_database))
    before = snapshot(staff_database)
    scan = images.scanner.scan
    count = 0

    def scan_fault(fd: int):  # type: ignore[no-untyped-def]
        nonlocal count
        count += 1
        if count == (1 if fault == "first_scan" else 2):
            raise FileRejected("SCANNER_FAILURE")
        return scan(fd)

    def fail(*args: object, **kwargs: object) -> None:
        raise SQLAlchemyError("synthetic failure")

    def broken_parser(fd: int, task: str, *, limits: Limits) -> bytes:
        raise FileRejected("PARSER_REJECTED")

    def broken_write(directory: int, key: str, data: bytes) -> None:
        raise OSError("synthetic write failure")

    actual_checked = images._checked
    checks = 0

    def bad_integrity(verified: int, receipts: int, key: str):  # type: ignore[no-untyped-def]
        nonlocal checks
        checks += 1
        if checks == 4:  # accept, prepare, current metadata, context finalizer
            raise FileRejected("FILE_CHANGED")
        return actual_checked(verified, receipts, key)

    with monkeypatch.context() as patch:
        if fault in {"first_scan", "second_scan"}:
            patch.setattr(images.scanner, "scan", scan_fault)
        elif fault == "decode":
            patch.setattr(pipeline_module, "run_parser", broken_parser)
        elif fault == "write":
            patch.setattr(pipeline_module, "create", broken_write)
        elif fault == "final_integrity":
            patch.setattr(images, "_checked", bad_integrity)
        elif fault == "audit":
            event.listen(AuditEvent, "before_insert", fail)
        elif fault == "commit":
            event.listen(staff_database.engine, "commit", fail)
        try:
            with pytest.raises((FileRejected, OSError, SQLAlchemyError)):
                replace(staff_database, identity, images, row, color="blue")
        finally:
            if fault == "audit":
                event.remove(AuditEvent, "before_insert", fail)
            if fault == "commit":
                event.remove(staff_database.engine, "commit", fail)
    assert snapshot(staff_database) == before
    with serving(images):
        assert read_client.get(url(row)).status_code == 200


@pytest.mark.parametrize("action", ["remove", "replace", "archive"])
def test_get_rechecks_after_concurrent_commit(
    read_client: TestClient,
    staff_database: Database,
    images: ImagePipeline,
    action: str,
    monkeypatch: MonkeyPatch,
) -> None:
    identity = actor(read_client, staff_database)
    row = replace(staff_database, identity, images, item(staff_database))
    ready, release = Event(), Event()
    original = images.prepare

    @contextmanager
    def paused(key: str):  # type: ignore[no-untyped-def]
        with original(key) as result:
            ready.set()
            assert release.wait(10)
            yield result

    with serving(images), ThreadPoolExecutor(max_workers=1) as pool:
        monkeypatch.setattr(images, "prepare", paused)
        reading = pool.submit(read_client.get, url(row))
        assert ready.wait(10)
        monkeypatch.setattr(images, "prepare", original)
        try:
            if action == "replace":
                changed = replace(staff_database, identity, images, row, color="blue")
            else:
                with staff_database.unit_of_work() as session:
                    if action == "remove":
                        CatalogImageService(session, identity, images).remove(
                            row.id, row.revision
                        )
                    else:
                        CatalogWriteService(session, identity).change_item_state(
                            row.id, ItemState(expected_revision=row.revision), "archive"
                        )
        finally:
            release.set()
        assert reading.result(timeout=10).status_code == 404
        assert read_client.get(url(row)).status_code == 404
        if action == "replace":
            assert read_client.get(url(changed)).status_code == 200


def test_concurrent_replacements_one_revision_wins(
    read_client: TestClient,
    staff_database: Database,
    images: ImagePipeline,
    monkeypatch: MonkeyPatch,
) -> None:
    identity = actor(read_client, staff_database)
    row = replace(staff_database, identity, images, item(staff_database))
    from threading import Barrier

    barrier = Barrier(2)
    original = images.prepare

    @contextmanager
    def simultaneous(key: str):  # type: ignore[no-untyped-def]
        with original(key) as result:
            barrier.wait(timeout=10)
            yield result

    monkeypatch.setattr(images, "prepare", simultaneous)

    def change(color: str) -> int:
        try:
            return replace(staff_database, identity, images, row, color=color).revision
        except CatalogRejected as error:
            assert error.status == 409
            return -1

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(change, ("blue", "green")))
    assert sorted(outcomes) == [-1, row.revision + 1]
    with staff_database.unit_of_work() as session:
        current = session.get(CatalogItem, row.id)
        assert current is not None and current.revision == row.revision + 1


@pytest.mark.parametrize("denial", ["disabled", "manager", "revoked", "expired"])
def test_authority_rechecked_after_scan(
    read_client: TestClient,
    staff_database: Database,
    images: ImagePipeline,
    denial: str,
    monkeypatch: MonkeyPatch,
) -> None:
    identity = actor(read_client, staff_database)
    row = replace(staff_database, identity, images, item(staff_database))
    scan = images.scanner.scan
    changed = False

    def revoke_after_scan(fd: int):  # type: ignore[no-untyped-def]
        nonlocal changed
        result = scan(fd)
        if not changed:
            with staff_database.unit_of_work() as session:
                if denial in {"disabled", "manager"}:
                    session.execute(
                        update(StaffUser)
                        .where(StaffUser.id == identity.user.id)
                        .values(
                            **(
                                {"is_active": False}
                                if denial == "disabled"
                                else {"role": "manager"}
                            )
                        )
                    )
                else:
                    session.execute(
                        update(StaffSession)
                        .where(StaffSession.id == identity.session.id)
                        .values(
                            **(
                                {"revoked_at": utc_now()}
                                if denial == "revoked"
                                else {"expires_at": utc_now() - timedelta(seconds=1)}
                            )
                        )
                    )
            changed = True
        return result

    monkeypatch.setattr(images.scanner, "scan", revoke_after_scan)
    before = snapshot(staff_database)
    with pytest.raises(CatalogRejected) as denied:
        replace(staff_database, identity, images, row, color="blue")
    assert denied.value.status == 403
    assert snapshot(staff_database) == before


def test_real_tcp_http_current_old_and_private_integrity(
    read_client: TestClient, staff_database: Database, images: ImagePipeline
) -> None:
    identity = actor(read_client, staff_database)
    first = replace(staff_database, identity, images, item(staff_database))
    with (
        live_http(staff_database, FilePipeline(images.root)) as browser,
        serving(images),
    ):
        assert browser.get(url(first)).status_code == 200
        second = replace(staff_database, identity, images, first, color="blue")
        assert browser.get(url(first)).status_code == 404
        assert browser.get(url(second)).status_code == 200
        assert second.image_key is not None
        path = images.root / "images/verified" / second.image_key
        path.chmod(0o600)
        path.write_bytes(picture(color="green"))
        path.chmod(0o400)
        assert browser.get(url(second)).status_code == 404


def test_archive_commits_while_replacement_is_preparing(
    read_client: TestClient,
    staff_database: Database,
    images: ImagePipeline,
    monkeypatch: MonkeyPatch,
) -> None:
    identity = actor(read_client, staff_database)
    row = replace(staff_database, identity, images, item(staff_database))
    ready, release = Event(), Event()
    original = images.prepare

    @contextmanager
    def paused(key: str):  # type: ignore[no-untyped-def]
        with original(key) as result:
            ready.set()
            assert release.wait(10)
            yield result

    monkeypatch.setattr(images, "prepare", paused)
    with ThreadPoolExecutor(max_workers=1) as pool:
        replacing = pool.submit(
            replace, staff_database, identity, images, row, "PNG", "blue"
        )
        assert ready.wait(10)
        try:
            with staff_database.unit_of_work() as session:
                archived = CatalogWriteService(session, identity).change_item_state(
                    row.id, ItemState(expected_revision=row.revision), "archive"
                )
        finally:
            release.set()
        with pytest.raises(CatalogRejected) as conflict:
            replacing.result(timeout=10)
        assert conflict.value.status == 409
    assert archived.image_key == row.image_key and archived.revision == row.revision + 1


def test_manager_and_inactive_identity_cannot_start_image_operation(
    read_client: TestClient, staff_database: Database, images: ImagePipeline
) -> None:
    identity = actor(read_client, staff_database, "manager")
    row = item(staff_database)
    with pytest.raises(CatalogRejected) as denied:
        replace(staff_database, identity, images, row)
    assert denied.value.status == 403
    assert not (images.root / "images").exists()
