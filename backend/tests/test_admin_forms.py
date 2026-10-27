"""The bounded receiver must reject before consuming untrusted oversized bodies."""

import asyncio

import pytest
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.types import Message

from app.admin.forms import bounded_body


@pytest.mark.parametrize("lengths", [[b"65"], [b"1", b"1"], [b"-1"], [b"invalid"]])
def test_declared_oversize_or_ambiguous_length_never_reads(
    lengths: list[bytes],
) -> None:
    async def receive() -> Message:
        raise AssertionError("The body must not be read")

    request = Request(
        {"type": "http", "headers": [(b"content-length", n) for n in lengths]}, receive
    )
    with pytest.raises(HTTPException) as denied:
        asyncio.run(bounded_body(request, 64))
    assert denied.value.status_code == (413 if lengths == [b"65"] else 422)


def test_chunked_limit_stops_receiving_and_never_caches_oversize() -> None:
    reads = 0

    async def receive() -> Message:
        nonlocal reads
        reads += 1
        assert reads <= 2
        return {"type": "http.request", "body": b"x" * 33, "more_body": True}

    request = Request({"type": "http", "headers": []}, receive)
    with pytest.raises(HTTPException) as denied:
        asyncio.run(bounded_body(request, 64))
    assert denied.value.status_code == 413 and reads == 2
    assert not hasattr(request, "_body")


def test_bounded_body_replays_exact_bytes_for_form_parser() -> None:
    chunks = iter([b"first", b"second"])

    async def receive() -> Message:
        chunk = next(chunks)
        return {"type": "http.request", "body": chunk, "more_body": chunk != b"second"}

    request = Request({"type": "http", "headers": []}, receive)
    asyncio.run(bounded_body(request, 11))
    assert asyncio.run(request.body()) == b"firstsecond"
