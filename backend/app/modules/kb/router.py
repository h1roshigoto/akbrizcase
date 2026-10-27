"""Published knowledge HTTP reads; bytes and all failures precede HTTP success."""

import hashlib
import os
from collections.abc import Iterator
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import ConfigDict
from sqlalchemy.orm import Session

from app.core.csrf import SESSION_COOKIE
from app.core.db import Database
from app.modules.kb.access import cookie_viewer
from app.modules.kb.files import FilePipeline, FileRejected
from app.modules.kb.files.formats import MIMES
from app.modules.kb.read_service import KnowledgeReadService, SourceNotFound
from app.schemas.common import ErrorEnvelope, UUIDValue
from app.schemas.documents import DocumentPage, DocumentQuery
from app.schemas.sources import KnowledgePage, KnowledgeQuery


class SearchParameters(KnowledgeQuery):
    model_config = ConfigDict(strict=False)


class DocumentParameters(DocumentQuery):
    model_config = ConfigDict(strict=False)


def knowledge_session(request: Request) -> Iterator[Session]:
    database: Database | None = request.app.state.database
    if database is None:
        raise HTTPException(503)
    with database.unit_of_work() as session:
        yield session


def read_service(
    request: Request,
    response: Response,
    session: Annotated[Session, Depends(knowledge_session, scope="function")],
) -> KnowledgeReadService:
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Vary"] = "Cookie"
    viewer = cookie_viewer(request.cookies.get(SESSION_COOKIE, ""))
    return KnowledgeReadService(session, viewer)


ReadService = Annotated[KnowledgeReadService, Depends(read_service)]
router = APIRouter(
    prefix="/api/v1",
    tags=["knowledge"],
    responses={
        404: {"model": ErrorEnvelope},
        422: {"model": ErrorEnvelope},
        503: {"model": ErrorEnvelope},
    },
)


@router.get("/knowledge/search", response_model=KnowledgePage)
def search(
    query: Annotated[SearchParameters, Query()], service: ReadService
) -> KnowledgePage:
    return service.search(query)


@router.get("/documents", response_model=DocumentPage)
def documents(
    query: Annotated[DocumentParameters, Query()], service: ReadService
) -> DocumentPage:
    return service.documents(query)


def file_pipeline() -> FilePipeline:
    return FilePipeline.from_environment()


@router.get("/documents/{id}/versions/{version_id}/file", response_class=Response)
def document_file(
    id: UUIDValue,
    version_id: UUIDValue,
    service: ReadService,
    pipeline: Annotated[FilePipeline, Depends(file_pipeline)],
) -> Response:
    try:
        version = service.exact_version(id, version_id)
        expected = (
            version.file_key,
            version.mime,
            version.size_bytes,
            version.checksum_sha256,
        )
        with pipeline.prepare(version.file_key) as (metadata, descriptor):
            if (
                metadata.file_key,
                metadata.mime,
                metadata.size_bytes,
                metadata.checksum_sha256,
            ) != expected:
                raise FileRejected("FILE_CHANGED")
            # Bounded at 25 MiB by the existing pipeline. Read the complete sealed
            # snapshot while its owner is alive; no deferred fd/path streaming.
            os.lseek(descriptor, 0, os.SEEK_SET)
            with os.fdopen(os.dup(descriptor), "rb") as snapshot:
                data = snapshot.read(metadata.size_bytes + 1)
            if (
                len(data) != metadata.size_bytes
                or hashlib.sha256(data).hexdigest() != metadata.checksum_sha256
            ):
                raise FileRejected("FILE_CHANGED")
        # prepare's exit checksum check also finished before any headers/bytes.
        # READ COMMITTED refresh catches a visibility change/revocation during AV.
        version = service.exact_version(id, version_id)
        if (
            version.file_key,
            version.mime,
            version.size_bytes,
            version.checksum_sha256,
        ) != expected:
            raise FileRejected("FILE_CHANGED")
    except SourceNotFound:
        raise HTTPException(404) from None
    except (FileRejected, OSError):
        raise HTTPException(503) from None
    extension = next(k for k, v in MIMES.items() if v == version.mime)
    label = " — предыдущая редакция" if version.status == "retired" else ""
    filename = quote(f"document-{version.version}{label}.{extension}")
    return Response(
        data,
        media_type=version.mime,
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{filename}",
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store",
            "Vary": "Cookie",
            "X-Document-Version-Status": version.status,
        },
    )
