"""Public catalog and staff security coexist in the production FastAPI app."""

import secrets
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import delete

from app.core.csrf import StaffSecurity
from app.core.db import Database
from app.core.staff import bootstrap_admin, create_staff
from app.main import admin, app
from app.modules.catalog.models import CatalogItem, EquipmentModel
from app.schemas.common import ErrorEnvelope

pytestmark = pytest.mark.integration
ORIGIN = "https://localhost:8443"
PASSWORD = "synthetic-combined-app-password"


def assert_error(response: Response, status: int, code: str) -> None:
    assert response.status_code == status, response.text
    body = ErrorEnvelope.model_validate_json(response.content)
    assert body.error.code == code and body.error.details == {}
    assert str(body.request_id) == response.headers["x-request-id"]
    assert UUID(response.headers["x-request-id"]).version == 4
    assert response.headers["cache-control"] == "no-store"
    assert "untrusted-request-id" not in response.text


@pytest.mark.parametrize("role", ["manager", "admin"])
def test_catalog_and_staff_in_production_app(
    staff_database: Database,
    databases: tuple[Database, Database],
    monkeypatch: pytest.MonkeyPatch,
    role: str,
) -> None:
    identifiers = [uuid4(), uuid4()]
    model_id = uuid4()
    with staff_database.unit_of_work() as session:
        assert bootstrap_admin(session, login="combined-admin", password=PASSWORD).ok
        assert create_staff(
            session,
            admin_login="combined-admin",
            admin_password=PASSWORD,
            login="combined-manager",
            password=PASSWORD,
            role="manager",
        ).ok
        session.add(
            EquipmentModel(
                id=model_id,
                code="COMBINED",
                name="Synthetic model",
                description="Synthetic coexistence fixture",
                status="published",
            )
        )
        for identifier, kind in zip(identifiers, ("product", "service"), strict=True):
            session.add(
                CatalogItem(
                    id=identifier,
                    sku=f"COMBINED-{kind}",
                    kind=kind,
                    name=f"Synthetic {kind}",
                    description="Synthetic coexistence fixture",
                    unit="unit",
                    price_mode="fixed" if kind == "product" else "on_request",
                    price=Decimal("123.45") if kind == "product" else None,
                    tax_note="TEST",
                    status="published",
                )
            )
    try:
        # Actual app, normal lifespan/runtime PostgreSQL and real staff services.
        # Only non-secret test origin/signing configuration is supplied here.
        with TestClient(app, base_url=ORIGIN, follow_redirects=False) as browser:
            monkeypatch.setattr(
                app.state,
                "staff_security",
                StaffSecurity(
                    frozenset({ORIGIN}),
                    secrets.token_hex(32),
                ),
            )
            # Service-backed order and admin-only catalog pages; no ORM CRUD.
            assert {view.identity for view in admin.views} == {
                "orders",
                "order-detail",
                "catalog-items",
                "equipment-models",
                "catalog-editor",
            }
            assert all(not view.is_model for view in admin.views)
            schema = browser.get("/openapi.json").json()["paths"]
            assert "post" in schema["/admin/auth/login"]
            assert "post" in schema["/admin/auth/logout"]
            catalog = browser.get("/api/v1/catalog/items")
            assert catalog.status_code == 200
            assert {row["kind"] for row in catalog.json()["items"]} == {
                "product",
                "service",
            }
            assert browser.get("/api/v1/equipment-models").json()["total"] == 1
            assert_error(browser.get("/admin/"), 401, "AUTHENTICATION_REQUIRED")

            csrf = browser.get("/admin/auth/csrf").json()["csrf_token"]
            response = browser.post(
                "/admin/auth/login",
                json={
                    "login": f"combined-{role}",
                    "password": PASSWORD,
                },
                headers={"Origin": ORIGIN, "X-CSRF-Token": csrf},
            )
            assert response.status_code == 200
            csrf = response.json()["csrf_token"]
            assert browser.get("/admin/").status_code == 200
            assert browser.get("/api/v1/catalog/items").json() == catalog.json()
            assert (
                browser.get(f"/api/v1/catalog/items/{identifiers[0]}").status_code
                == 200
            )
            for path, status, code in (
                (f"/api/v1/catalog/items/{uuid4()}", 404, "NOT_FOUND"),
                ("/api/v1/not-implemented", 404, "NOT_FOUND"),
                ("/api/v1/catalog/items?limit=101", 422, "VALIDATION_ERROR"),
            ):
                assert_error(
                    browser.get(
                        path,
                        headers={
                            "X-Request-ID": "untrusted-request-id",
                        },
                    ),
                    status,
                    code,
                )
            assert_error(
                browser.post(
                    "/admin/auth/logout",
                    headers={
                        "Origin": ORIGIN,
                        "X-CSRF-Token": "forged",
                        "X-Request-ID": "untrusted-request-id",
                    },
                ),
                403,
                "CSRF_DENIED",
            )
            with monkeypatch.context() as outage:
                outage.setattr(app.state, "database", None)
                assert_error(
                    browser.get("/api/v1/catalog/items"), 503, "SERVICE_UNAVAILABLE"
                )
                assert_error(browser.get("/admin/"), 503, "AUTH_UNAVAILABLE")
            assert browser.get("/admin/").status_code == 200
            assert (
                browser.post(
                    "/admin/auth/logout",
                    headers={
                        "Origin": ORIGIN,
                        "X-CSRF-Token": csrf,
                    },
                ).status_code
                == 204
            )
            assert_error(browser.get("/admin/"), 401, "AUTHENTICATION_REQUIRED")
            assert browser.get("/api/v1/catalog/items").json() == catalog.json()
    finally:
        _, migrator = databases
        with migrator.unit_of_work() as session:
            session.execute(delete(CatalogItem).where(CatalogItem.id.in_(identifiers)))
            session.execute(delete(EquipmentModel).where(EquipmentModel.id == model_id))
