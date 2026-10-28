"""Public chat ownership is independent of administrative authentication."""

from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Query, Request

from app.core.db import Database
from app.modules.chat.access import ChatExpired, ChatNotFound
from app.modules.chat.service import create_session, read_session
from app.schemas.chat import ChatSessionCreate, ChatSessionCreated, ChatSessionRead
from app.schemas.common import ErrorEnvelope, UUIDValue

router = APIRouter(
    prefix="/api/v1/chat/sessions",
    tags=["chat"],
    responses={
        404: {"model": ErrorEnvelope},
        410: {"model": ErrorEnvelope},
        422: {"model": ErrorEnvelope},
        503: {"model": ErrorEnvelope},
    },
)


def database_for(request: Request) -> Database:
    database: Database | None = request.app.state.database
    if database is None:
        raise HTTPException(503)
    return database


@router.post("", status_code=201, response_model=ChatSessionCreated)
def create(request: Request, body: ChatSessionCreate) -> ChatSessionCreated:
    with database_for(request).unit_of_work() as session:
        result = create_session(session, body)
    # UoW must commit successfully before the sole disclosure of the raw token.
    return result


@router.get("/{identifier}", response_model=ChatSessionRead)
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
            return read_session(session, identifier, token, before)
    except ChatNotFound:
        raise HTTPException(404) from None
    except ChatExpired:
        raise HTTPException(410) from None
