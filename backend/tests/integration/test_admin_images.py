"""Real staff cookies, strict multipart, PostgreSQL, ClamAV and image sandbox."""

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import event, select, text, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from test_catalog_image_binding import images as images
from test_catalog_image_binding import item, picture, serving, url
from test_order_reads import ORIGIN, business_rows, create, login
from test_order_reads import read_client as read_client

from app.core.db import Database
from app.core.db_types import utc_now
from app.core.staff import lock_staff
from app.core.staff_models import AuditEvent, StaffSession, StaffUser
from app.modules.catalog.images import pipeline as pipeline_module
from app.modules.catalog.images.pipeline import MAX_BYTES, ImagePipeline
from app.modules.catalog.models import CatalogItem
from app.modules.catalog.write_service import CatalogWriteService
from app.modules.kb.files import FileRejected

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def image_adapter(
    images: ImagePipeline, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    monkeypatch.setattr(
        ImagePipeline, "from_environment", classmethod(lambda cls: images)
    )
    with serving(images):
        yield


def upload(
    client: TestClient,
    row: CatalogItem,
    csrf: str,
    *,
    fmt: str = "PNG",
    color: str = "red",
) -> Response:
    return client.post(
        f"/admin/catalog/items/{row.id}/image/replace",
        data={"csrf_token": csrf, "expected_revision": str(row.revision)},
        files={
            "image": (
                "synthetic." + fmt.lower(),
                picture(fmt, color),
                "image/jpeg" if fmt == "JPEG" else "image/png",
            )
        },
        headers={"Origin": ORIGIN},
    )


def current(database: Database, row: CatalogItem) -> CatalogItem:
    with database.unit_of_work() as session:
        result = session.get(CatalogItem, row.id)
        assert result is not None
        return result


@pytest.mark.parametrize(("kind", "fmt"), [("product", "JPEG"), ("service", "PNG")])
def test_admin_install_replace_remove(
    read_client: TestClient,
    staff_database: Database,
    images: ImagePipeline,
    kind: str,
    fmt: str,
) -> None:
    csrf = login(read_client, "admin")
    create(read_client, staff_database)
    orders = business_rows(staff_database)
    row = item(staff_database, kind)
    base = f"/admin/catalog/items/{row.id}/image"
    page = read_client.get(base)
    assert page.status_code == 200 and f'value="{row.revision}"' in page.text
    assert upload(read_client, row, csrf, fmt=fmt).status_code == 303
    first = current(staff_database, row)
    assert first.revision == row.revision + 1 and first.updated_at > row.updated_at
    assert read_client.get(url(first)).status_code == 200
    assert read_client.get(f"/api/v1/catalog/items/{row.id}").json()[
        "image_url"
    ] == url(first)
    assert upload(read_client, first, csrf, fmt=fmt).status_code == 303
    assert current(staff_database, row).revision == first.revision
    assert upload(read_client, first, csrf, fmt=fmt, color="blue").status_code == 303
    second = current(staff_database, row)
    assert second.revision == first.revision + 1 and second.image_key != first.image_key
    assert read_client.get(url(first)).status_code == 404
    assert read_client.get(url(second)).status_code == 200
    assert upload(read_client, first, csrf, fmt=fmt, color="green").status_code == 409
    assert current(staff_database, row).image_key == second.image_key
    for revision in (second.revision, second.revision + 1):
        assert (
            read_client.post(
                base + "/remove",
                data={"csrf_token": csrf, "expected_revision": str(revision)},
                headers={"Origin": ORIGIN},
            ).status_code
            == 303
        )
    removed = current(staff_database, row)
    assert removed.image_key is None and removed.revision == second.revision + 1
    assert read_client.get(url(second)).status_code == 404
    assert (
        read_client.get(f"/api/v1/catalog/items/{row.id}").json()["image_url"] is None
    )
    assert (images.root / "images/verified" / str(first.image_key)).is_file()
    assert (images.root / "images/verified" / str(second.image_key)).is_file()
    assert orders == business_rows(staff_database)
    with staff_database.unit_of_work() as session:
        audits = session.scalars(
            select(AuditEvent).where(
                AuditEvent.entity_id == row.id, AuditEvent.action == "catalog_item.edit"
            )
        ).all()
        assert len(audits) == 3 and all(a.actor_id is not None for a in audits)


@pytest.mark.parametrize(
    "case",
    [
        "missing_csrf",
        "forged_csrf",
        "duplicate_csrf",
        "actor_id",
        "image_key",
        "safe",
        "duplicate_revision",
        "bad_revision",
        "file_revision",
        "two_files",
        "extra_file",
        "huge_field",
        "oversize",
        "huge_body",
        "bad_mime",
        "bad_extension",
        "bad_signature",
        "origin",
        "host",
    ],
)
def test_upload_rejects_untrusted_forms(
    read_client: TestClient, staff_database: Database, case: str
) -> None:
    csrf = login(read_client, "admin")
    row = item(staff_database)
    fields: list[tuple[str, tuple[None, str] | tuple[str, bytes, str]]] = [
        ("csrf_token", (None, csrf)),
        ("expected_revision", (None, str(row.revision))),
        ("image", ("a.png", picture(), "image/png")),
    ]
    status = 422
    headers = {"Origin": ORIGIN}
    if case == "missing_csrf":
        fields.pop(0)
        status = 403
    elif case == "forged_csrf":
        fields[0] = ("csrf_token", (None, "forged"))
        status = 403
    elif case == "duplicate_csrf":
        fields.insert(0, fields[0])
        status = 403
    elif case in {"actor_id", "image_key", "safe"}:
        fields.append((case, (None, str(uuid4()))))
    elif case == "duplicate_revision":
        fields.append(fields[1])
    elif case == "bad_revision":
        fields[1] = ("expected_revision", (None, "1.0"))
    elif case == "file_revision":
        fields[1] = ("expected_revision", ("x.txt", b"1", "text/plain"))
        status = 400
    elif case in {"two_files", "extra_file"}:
        fields.append(
            (
                "image" if case == "two_files" else "extra",
                ("a.png", picture(), "image/png"),
            )
        )
        status = 400
    elif case == "huge_field":
        fields[1] = ("expected_revision", (None, "1" * 1025))
        status = 400
    elif case in {"oversize", "huge_body"}:
        fields[2] = (
            "image",
            (
                "a.png",
                b"x" * (MAX_BYTES + (1 if case == "oversize" else 20000)),
                "image/png",
            ),
        )
        status = 413
    elif case == "bad_mime":
        fields[2] = ("image", ("a.png", picture(), "image/jpeg"))
    elif case == "bad_extension":
        fields[2] = ("image", ("a.svg", picture(), "image/png"))
    elif case == "bad_signature":
        fields[2] = ("image", ("a.png", b"not an image", "image/png"))
    elif case in {"origin", "host"}:
        headers[case.title()] = (
            "https://evil.invalid" if case == "origin" else "evil.invalid"
        )
        status = 403
    response = read_client.post(
        f"/admin/catalog/items/{row.id}/image/replace", files=fields, headers=headers
    )
    assert response.status_code == status, response.text
    after = current(staff_database, row)
    assert (after.image_key, after.revision, after.updated_at) == (
        row.image_key,
        row.revision,
        row.updated_at,
    )
    assert "clam" not in response.text.lower() and "/var/" not in response.text


@pytest.mark.parametrize("role", ["anonymous", "manager"])
def test_new_direct_routes_require_admin(
    read_client: TestClient, staff_database: Database, role: str
) -> None:
    csrf = login(read_client, "manager") if role == "manager" else "forged"
    row = item(staff_database)
    status = 403 if role == "manager" else 401
    base = f"/admin/catalog/items/{row.id}"
    for path in ("/image", "/applicability"):
        assert read_client.get(base + path).status_code == status
    assert upload(read_client, row, csrf).status_code == status
    for path in (
        "/image/remove",
        "/applicability/candidate",
        "/applicability/review",
        "/applicability/publish",
        "/applicability/revoke",
        "/applicability/preview",
    ):
        assert (
            read_client.post(
                base + path, data={"csrf_token": csrf}, headers={"Origin": ORIGIN}
            ).status_code
            == status
        )


@pytest.mark.parametrize("failure", ["av", "pillow", "audit", "commit"])
def test_failed_replace_keeps_previous_http_image(
    read_client: TestClient,
    staff_database: Database,
    images: ImagePipeline,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    csrf = login(read_client, "admin")
    row = item(staff_database)
    assert upload(read_client, row, csrf).status_code == 303
    old = current(staff_database, row)
    original = read_client.get(url(old)).content

    def denied(*args: object, **kwargs: object) -> None:
        raise FileRejected("SYNTHETIC_FAILURE")

    def commit(session: Session) -> None:
        # Initial authentication commits too. Fail only the binding transaction.
        if session.info.get("image_binding") and not session.in_nested_transaction():
            raise SQLAlchemyError("SYNTHETIC_COMMIT")

    if failure == "av":
        monkeypatch.setattr(images.scanner, "scan", denied)
    elif failure == "pillow":
        monkeypatch.setattr(pipeline_module, "run_parser", denied)
    elif failure == "audit":
        monkeypatch.setattr(
            CatalogWriteService,
            "_audit",
            lambda *args: (_ for _ in ()).throw(SQLAlchemyError("SYNTHETIC_AUDIT")),
        )
    else:
        changed = CatalogWriteService._changed

        def mark(self: CatalogWriteService, *args: object) -> None:
            self.session.info["image_binding"] = True
            changed(self, *args)  # type: ignore[arg-type]

        monkeypatch.setattr(CatalogWriteService, "_changed", mark)
        event.listen(staff_database.sessions, "before_commit", commit)
    try:
        assert upload(read_client, old, csrf, color="blue").status_code in (422, 503)
    finally:
        if failure == "commit":
            event.remove(staff_database.sessions, "before_commit", commit)
        monkeypatch.undo()
    after = current(staff_database, row)
    assert (after.image_key, after.revision, after.updated_at) == (
        old.image_key,
        old.revision,
        old.updated_at,
    )
    assert read_client.get(url(old)).content == original
    with staff_database.unit_of_work() as session:
        assert (
            len(
                session.scalars(
                    select(AuditEvent).where(
                        AuditEvent.entity_id == row.id,
                        AuditEvent.action == "catalog_item.edit",
                    )
                ).all()
            )
            == 1
        )


@pytest.mark.parametrize("change", ["none", "disable", "demote", "revoke", "expire"])
def test_processing_releases_staff_lock_and_rechecks_identity(
    read_client: TestClient,
    staff_database: Database,
    images: ImagePipeline,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    csrf = login(read_client, "admin")
    row = item(staff_database)
    entered, resume = Event(), Event()
    scan = images.scanner.scan
    first = True

    def pause(*args: object, **kwargs: object) -> object:
        nonlocal first
        if first:
            first = False
            entered.set()
            assert resume.wait(5)
        return scan(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(images.scanner, "scan", pause)
    with ThreadPoolExecutor(max_workers=1) as workers:
        response = workers.submit(upload, read_client, row, csrf)
        try:
            assert entered.wait(5)
            with staff_database.unit_of_work() as session:
                session.execute(text("SET LOCAL lock_timeout='200ms'"))
                lock_staff(session)
                assert (
                    session.scalar(
                        text(
                            "SELECT count(*) FROM pg_stat_activity "
                            "WHERE datname=current_database() "
                            "AND pid <> pg_backend_pid() "
                            "AND state='idle in transaction'"
                        )
                    )
                    == 0
                )
                user = session.scalar(
                    select(StaffUser).where(StaffUser.login == "read-admin")
                )
                assert user is not None
                if change == "disable":
                    user.is_active = False
                elif change == "demote":
                    user.role = "manager"
                elif change in {"revoke", "expire"}:
                    session.execute(
                        update(StaffSession)
                        .where(StaffSession.user_id == user.id)
                        .values(
                            **(
                                {"revoked_at": utc_now()}
                                if change == "revoke"
                                else {"expires_at": utc_now()}
                            )
                        )
                    )
            # Commit authority changes while ClamAV is paused, then finish file work.
        finally:
            resume.set()
        result = response.result(timeout=10)
    assert result.status_code == (303 if change == "none" else 403), result.text
    after = current(staff_database, row)
    assert after.revision == row.revision + (1 if change == "none" else 0)


def test_draft_image_preview_is_admin_only(
    read_client: TestClient, staff_database: Database
) -> None:
    csrf = login(read_client, "admin")
    row = item(staff_database)
    with staff_database.unit_of_work() as session:
        session.execute(
            update(CatalogItem).where(CatalogItem.id == row.id).values(status="draft")
        )
    assert upload(read_client, row, csrf).status_code == 303
    changed = current(staff_database, row)
    path = f"/admin/catalog/items/{row.id}/image/preview"
    assert read_client.get(url(changed)).status_code == 404
    response = read_client.get(path)
    assert (
        response.status_code == 200 and response.headers["content-type"] == "image/png"
    )
    assert response.headers["cache-control"] == "no-store"
    login(read_client, "manager")
    assert read_client.get(path).status_code == 403
    read_client.cookies.clear()
    assert read_client.get(path).status_code == 401


def test_concurrent_http_uploads_have_one_winner(
    read_client: TestClient,
    staff_database: Database,
    images: ImagePipeline,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from threading import Barrier, local

    csrf = login(read_client, "admin")
    row = item(staff_database)
    barrier, thread = Barrier(2), local()
    scan = images.scanner.scan

    def concurrent(*args: object, **kwargs: object) -> object:
        if not getattr(thread, "entered", False):
            thread.entered = True
            barrier.wait(timeout=5)
        return scan(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(images.scanner, "scan", concurrent)
    with ThreadPoolExecutor(max_workers=2) as workers:
        uploads = [
            workers.submit(upload, read_client, row, csrf, color=color)
            for color in ("blue", "green")
        ]
        results = [pending.result(timeout=15).status_code for pending in uploads]
    assert sorted(results) == [303, 409]
    after = current(staff_database, row)
    assert after.revision == row.revision + 1 and after.image_key
    with staff_database.unit_of_work() as session:
        assert (
            len(
                session.scalars(
                    select(AuditEvent).where(
                        AuditEvent.entity_id == row.id,
                        AuditEvent.action == "catalog_item.edit",
                    )
                ).all()
            )
            == 1
        )
