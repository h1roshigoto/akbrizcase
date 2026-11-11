"""HTTP confirmation binding, strict forms and safe technical projections."""

import asyncio
from urllib.parse import urlencode
from uuid import uuid4

import pytest
from fastapi import Request
from starlette.routing import Route

from app.admin.content import (
    DocumentLifecycleView,
    DocumentsView,
    action_fields,
    already_applied,
    dependency_summaries,
    offset,
    preview_binding,
    preview_digest,
)
from app.admin.sqladmin import StaffBaseView
from app.core.csrf import SESSION_COOKIE, StaffSecurity
from app.main import admin
from app.modules.kb.lifecycle_service import LifecycleReport

SECURITY = StaffSecurity(frozenset({"https://localhost:8443"}), "synthetic-key" * 4)
COOKIE = "a" * 64


def request(query: str = "", body: str = "", cookie: str = COOKIE) -> Request:
    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": body.encode(), "more_body": False}

    value = Request(
        {
            "type": "http",
            "method": "POST",
            "query_string": query.encode(),
            "headers": [
                (b"cookie", f"{SESSION_COOKIE}={cookie}".encode()),
                (b"content-type", b"application/x-www-form-urlencoded"),
            ],
        },
        receive,
    )
    value.state.staff_security = SECURITY
    return value


@pytest.mark.parametrize(
    "query",
    ["offset=-1", "offset=10001", "offset=1&offset=2", "limit=1000", "offset=١"],
)
def test_document_pagination_rejects_ambiguous_or_unbounded_input(query: str) -> None:
    with pytest.raises(ValueError):
        offset(request(query))
    assert offset(request("offset=10000")) == 10000


def test_confirmation_cannot_be_retargeted_or_reused_by_another_session() -> None:
    document, version = uuid4(), uuid4()
    digest = preview_digest(LifecycleReport("PREVIEW", {"status": "active"}))
    original = request()
    binding = preview_binding(original, document, version, "revoke", digest)
    token = SECURITY.issue(binding, "kb-preview")
    assert SECURITY.verify(token, binding, "kb-preview", 600)
    for other in (
        preview_binding(original, uuid4(), version, "revoke", digest),
        preview_binding(original, document, uuid4(), "revoke", digest),
        preview_binding(original, document, None, "revoke", digest),
        preview_binding(original, document, version, "publish", digest),
        preview_binding(original, document, version, "revoke", "f" * 64),
        preview_binding(request(cookie="b" * 64), document, version, "revoke", digest),
    ):
        assert not SECURITY.verify(token, other, "kb-preview", 600)
    assert not SECURITY.verify(token, binding, "session", 600)
    assert not SECURITY.verify(token + "tampered", binding, "kb-preview", 600)
    assert not SECURITY.verify(token, binding, "kb-preview", -1)


@pytest.mark.parametrize(
    "extra",
    [
        [("confirm", "revoke"), ("confirm", "publish")],
        [("force", "true")],
        [("actor_id", str(uuid4()))],
        [("password", "private")],
        [("csrf_token", "duplicate")],
    ],
)
def test_forms_reject_duplicates_and_bypass_fields(
    extra: list[tuple[str, str]],
) -> None:
    body = urlencode([("csrf_token", SECURITY.issue(COOKIE, "session")), *extra])
    with pytest.raises(ValueError):
        asyncio.run(action_fields(request(body=body), {"confirm"}))


def test_preview_html_projection_is_bounded_and_does_not_dump_payloads() -> None:
    ids = [str(uuid4()) for _ in range(60)]
    report = LifecycleReport(
        "PREVIEW",
        {
            "file_key": "PRIVATE-KEY",
            "dependencies": [
                {
                    "kind": "catalog_item_models.evidence_section_id",
                    "count": 60,
                    "ids": ids,
                    "link_count": 80,
                    "links": [{"private": "PRIVATE-TEXT"}],
                    "arbitrary": "PRIVATE-PAYLOAD",
                },
                {"kind": "PRIVATE-PROVIDER", "text": "PRIVATE-TEXT"},
            ],
        },
    )
    projected = dependency_summaries(report)
    assert len(projected) == 1
    assert projected[0]["ids"] == ids[:50]
    assert projected[0]["count"] == 60 and projected[0]["link_count"] == 80
    assert "PRIVATE" not in str(projected)


def test_all_content_routes_use_admin_post_boundary_and_no_generic_crud() -> None:
    assert issubclass(DocumentsView, StaffBaseView)
    assert issubclass(DocumentLifecycleView, StaffBaseView)
    assert DocumentsView.required_role == DocumentLifecycleView.required_role == "admin"
    routes = {
        route.path: route.methods
        for route in admin.admin.routes
        if isinstance(route, Route) and route.path.startswith("/documents")
    }
    assert routes == {
        "/documents": {"GET", "HEAD"},
        "/documents/{document}": {"GET", "HEAD"},
        "/documents/{document}/preview": {"POST"},
        "/documents/{document}/revoke": {"POST"},
        "/documents/{document}/versions/{version}/{operation}": {"POST"},
    }


def test_repeat_confirmation_allows_existing_noop_without_restoring_state() -> None:
    version = uuid4()
    report = LifecycleReport(
        "PREVIEW",
        {
            "status": "active",
            "versions": [{"version_id": str(version), "status": "revoked"}],
        },
    )
    assert already_applied(report, "revoke", version)
    assert not already_applied(report, "publish", version)
    assert not already_applied(report, "revoke", None)
    assert not already_applied(report, "revoke", uuid4())
