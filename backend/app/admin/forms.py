"""Bounded, unambiguous HTTP forms; all values remain untrusted."""

from fastapi import Request
from starlette.datastructures import FormData
from starlette.exceptions import HTTPException


async def bounded_body(request: Request, maximum: int) -> None:
    lengths = request.headers.getlist("content-length")
    if lengths and (
        len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdecimal()
    ):
        raise HTTPException(422, "Invalid body")
    if lengths and int(lengths[0]) > maximum:
        raise HTTPException(413, "Body too large")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > maximum:
            raise HTTPException(413, "Body too large")
        body.extend(chunk)
    # Starlette replays these bounded bytes for form parsing / the mounted app.
    request._body = bytes(body)


def unique_fields(form: FormData, allowed: set[str]) -> dict[str, str]:
    if set(form) - allowed or any(len(form.getlist(k)) != 1 for k in form):
        raise ValueError("Unexpected or duplicate field")
    values = dict(form)
    if any(not isinstance(v, str) for v in values.values()):
        raise ValueError("Expected text fields")
    if any(len(str(v).encode("utf-8")) > 1024 for v in values.values()):
        raise ValueError("Text field too large")
    return {k: str(v) for k, v in values.items()}


def revision(value: str) -> int:
    if not value.isascii() or not value.isdecimal() or len(value) > 10:
        raise ValueError("Invalid revision")
    result = int(value)
    if not 1 <= result <= 2147483647:
        raise ValueError("Invalid revision")
    return result
