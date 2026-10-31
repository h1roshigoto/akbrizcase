"""Public chat ownership is independent of administrative authentication."""

from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from app.core.api_errors import error_response
from app.core.db import Database
from app.core.http_security import ip_subjects
from app.core.rate_limits import check_limit
from app.modules.chat.access import ChatExpired, ChatNotFound
from app.modules.chat.message_request import parse_message_json
from app.modules.chat.message_state import StoredStateUnavailable
from app.modules.chat.messages import (
    MessageConflict,
    create_message,
    read_message_result,
)
from app.modules.chat.projection import begin_projection_read
from app.modules.chat.service import create_session, read_session
from app.modules.kb.tree_reads import published_scenarios
from app.modules.orders.service import OrderRejected, collection_gate
from app.schemas.chat import (
    ChatMessageCreate,
    ChatMessageResult,
    ChatScenario,
    ChatScenarios,
    ChatSessionCreate,
    ChatSessionCreated,
    ChatSessionRead,
)
from app.schemas.common import ErrorEnvelope, UUIDValue

router = APIRouter(
    prefix="/api/v1/chat",
    tags=["chat"],
    responses={
        404: {"model": ErrorEnvelope},
        409: {"model": ErrorEnvelope},
        410: {"model": ErrorEnvelope},
        422: {"model": ErrorEnvelope},
        429: {"model": ErrorEnvelope},
        503: {"model": ErrorEnvelope},
    },
)


def database_for(request: Request) -> Database:
    database: Database | None = request.app.state.database
    if database is None:
        raise HTTPException(503)
    return database


@router.get("/scenarios", response_model=ChatScenarios)
def scenarios(request: Request, model_id: UUIDValue) -> ChatScenarios:
    with database_for(request).unit_of_work() as session:
        begin_projection_read(session)
        result = ChatScenarios(
            items=[
                ChatScenario(
                    key=choice.key, title=choice.title, model_id=choice.model_id
                )
                for choice in published_scenarios(session, model_id)
            ]
        )
    return result


@router.post("/sessions", status_code=201, response_model=ChatSessionCreated)
def create(request: Request, body: ChatSessionCreate) -> ChatSessionCreated:
    try:
        with database_for(request).unit_of_work() as session:
            collection_gate(session)
            check_limit(session, "session", ip_subjects(request, "session", session))
            result = create_session(session, body)
    except OrderRejected:
        raise HTTPException(503) from None
    # UoW must commit successfully before the sole disclosure of the raw token.
    return result


@router.get("/sessions/{identifier}", response_model=ChatSessionRead)
def read(
    request: Request,
    identifier: UUIDValue,
    before: Annotated[UUIDValue | None, Query()] = None,
    chat_token: Annotated[str | None, Header(alias="X-Chat-Token")] = None,
) -> ChatSessionRead:
    # Reject duplicate headers rather than ambiguously picking an owner token.
    token = chat_token if len(request.headers.getlist("X-Chat-Token")) == 1 else None
    try:
        with database_for(request).unit_of_work() as session:
            begin_projection_read(session)
            return read_session(session, identifier, token, before)
    except ChatNotFound:
        raise HTTPException(404) from None
    except ChatExpired:
        raise HTTPException(410) from None


def persist_message(
    request: Request, body: ChatMessageCreate, token: str | None
) -> ChatMessageResult:
    with database_for(request).unit_of_work() as session:
        message_id = create_message(session, body, token)
    # A failed flush/COMMIT must never emit an HTTP success.
    # Fresh snapshot after COMMIT: dependency validation in the writer's UoW
    # does not authorize disclosure. Never reroute or apply an input here.
    with database_for(request).unit_of_work() as session:
        begin_projection_read(session)
        return read_message_result(session, body.session_id, message_id, token)


@router.post(
    "/messages",
    response_model=ChatMessageResult,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": ChatMessageCreate.model_json_schema(
                        ref_template=(
                            "#/paths/~1api~1v1~1chat~1messages/post/requestBody/content/"
                            "application~1json/schema/$defs/{model}"
                        )
                    )
                }
            },
        }
    },
)
async def message(
    request: Request,
    chat_token: Annotated[str | None, Header(alias="X-Chat-Token")] = None,
) -> ChatMessageResult | JSONResponse:
    raw = bytearray()
    try:
        if (
            request.headers.get("content-type", "").split(";", 1)[0].strip()
            != "application/json"
        ):
            raise ValueError
        # Same bounded raw-JSON boundary as orders: framework parsing alone
        # loses duplicate keys and can fail before we sanitize damaged payloads.
        async for chunk in request.stream():
            if len(raw) + len(chunk) > 16 * 1024:
                raise ValueError
            raw.extend(chunk)
        body = parse_message_json(bytes(raw))
    except (ValueError, RecursionError):
        return error_response(
            request, 422, "VALIDATION_ERROR", "Invalid request parameters"
        )
    token = chat_token if len(request.headers.getlist("X-Chat-Token")) == 1 else None
    try:
        return await run_in_threadpool(persist_message, request, body, token)
    except ChatNotFound:
        raise HTTPException(404) from None
    except ChatExpired:
        raise HTTPException(410) from None
    except MessageConflict as error:
        return error_response(request, 409, error.code, "Message conflict")
    except (StoredStateUnavailable, OrderRejected):
        raise HTTPException(503) from None
