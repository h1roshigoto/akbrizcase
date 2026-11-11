"""Fast checks for the staff form's bounded, unambiguous input contract."""

import asyncio
from urllib.parse import urlencode

import pytest
from fastapi import FastAPI
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.routing import Route
from starlette.types import Message

from app.admin.sqladmin import install_sqladmin
from app.admin.staff import FORM_BYTES, StaffView, staff_fields


def request_for(body: bytes, query: bytes = b"", content_type: str = "") -> Request:
    async def receive() -> Message:
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "POST",
            "query_string": query,
            "headers": [
                (
                    b"content-type",
                    (content_type or "application/x-www-form-urlencoded").encode(),
                )
            ],
        },
        receive,
    )


@pytest.mark.parametrize(
    "pairs",
    [
        [("login", "one"), ("login", "two")],
        [("csrf_token", "one"), ("csrf_token", "two")],
        [("actor_id", "spoof")],
        [("password_hash", "spoof")],
        [("password", "я" * 513)],
    ],
)
def test_ambiguous_unknown_and_large_fields(pairs: list[tuple[str, str]]) -> None:
    request = request_for(urlencode(pairs).encode())
    with pytest.raises(ValueError):
        asyncio.run(staff_fields(request, {"login", "password"}))


def test_form_preserves_password_spaces_and_utf8_boundary() -> None:
    password = " " + "я" * 511 + " "
    request = request_for(urlencode({"password": password}).encode())
    assert asyncio.run(staff_fields(request, {"password"})) == {"password": password}


def test_body_limit_even_without_content_length() -> None:
    with pytest.raises(HTTPException) as error:
        asyncio.run(staff_fields(request_for(b"x" * (FORM_BYTES + 1)), {"login"}))
    assert error.value.status_code == 413


@pytest.mark.parametrize(
    ("query", "content_type"),
    [(b"login=spoof", ""), (b"", "application/json"), (b"", "multipart/form-data")],
)
def test_reject_query_and_non_urlencoded_body(query: bytes, content_type: str) -> None:
    with pytest.raises(ValueError):
        asyncio.run(staff_fields(request_for(b"", query, content_type), {"login"}))


def test_staff_menu_and_explicit_methods() -> None:
    admin = install_sqladmin(FastAPI())
    view = next(view for view in admin.views if isinstance(view, StaffView))
    assert view.identity == "staff" and view.required_role == "admin"
    routes = {
        route.path: route.methods
        for route in admin.admin.routes
        if isinstance(route, Route) and route.path.startswith("/staff")
    }
    assert routes == {
        "/staff": {"GET", "HEAD"},
        "/staff/create": {"POST"},
        "/staff/{record}": {"GET", "HEAD"},
        "/staff/{record}/disable": {"POST"},
    }
