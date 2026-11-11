"""Narrow lifecycle forms over the existing staff UoW and KB services."""

import hashlib
import json
import re
from typing import Literal, cast
from uuid import UUID

from fastapi import Request
from sqladmin import expose
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.responses import Response

from app.admin.forms import unique_fields
from app.admin.security import error_response, request_identity, security_settings
from app.admin.sqladmin import StaffBaseView
from app.core.csrf import SESSION_COOKIE
from app.core.staff_sessions import SESSION_SECONDS
from app.modules.kb.files import FilePipeline, FileRejected
from app.modules.kb.lifecycle_service import LifecycleReport, run_lifecycle_as_actor
from app.modules.kb.models import Document, DocumentVersion
from app.schemas.common import parse_uuid

Action = Literal["publish", "revoke"]
PREVIEW_SECONDS = 600
PAGE_SIZE = 25


def offset(request: Request) -> int:
    query = request.query_params
    if set(query) - {"offset"} or len(query.getlist("offset")) > 1:
        raise ValueError("Invalid pagination")
    value = query.get("offset", "0")
    if not value.isascii() or not value.isdecimal() or len(value) > 5:
        raise ValueError("Invalid pagination")
    result = int(value)
    if result > 10000:
        raise ValueError("Invalid pagination")
    return result


def preview_digest(report: LifecycleReport) -> str:
    return hashlib.sha256(
        json.dumps(report.technical, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def preview_binding(
    request: Request,
    document: UUID,
    version: UUID | None,
    operation: Action,
    digest: str,
) -> str:
    # Purpose-separated, timed signature. No raw credential or payload in HTML.
    return ":".join(
        (
            request.cookies[SESSION_COOKIE],
            str(document),
            str(version),
            operation,
            digest,
        )
    )


def dependency_summaries(report: LifecycleReport) -> list[dict[str, object]]:
    """Explicit output projection: never render arbitrary service/JSONB payloads."""
    kinds = {
        "equipment_models.manual_document_id": "Historical equipment manual links",
        "catalog_item_models.evidence_section_id": "Affected applicability permissions",
        "diag_tree_sources.section_id": "Diagnostic trees rechecked at read time",
    }
    result: list[dict[str, object]] = []
    dependencies = report.technical.get("dependencies", [])
    assert isinstance(dependencies, list)
    for entry in dependencies:
        kind = entry["kind"]
        if kind not in kinds:
            continue
        result.append(
            {
                "label": kinds[kind],
                "count": int(entry["count"]),
                "link_count": int(entry.get("link_count", 0)),
                "ids": [str(UUID(value)) for value in entry["ids"][:50]],
            }
        )
    return result


def already_applied(
    report: LifecycleReport, operation: Action, version: UUID | None
) -> bool:
    if version is None:
        return operation == "revoke" and report.technical["status"] == "revoked"
    versions = report.technical["versions"]
    assert isinstance(versions, list)
    return any(
        row["version_id"] == str(version)
        and row["status"] == ("published" if operation == "publish" else "revoked")
        for row in versions
    )


async def action_fields(request: Request, allowed: set[str]) -> dict[str, str]:
    if (
        request.query_params
        or request.headers.get("content-type", "").split(";")[0]
        != "application/x-www-form-urlencoded"
    ):
        raise ValueError("Invalid form")
    async with request.form(max_files=0, max_fields=6, max_part_size=1024) as form:
        values = unique_fields(form, allowed | {"csrf_token"})
    csrf = values.pop("csrf_token", "")
    headers = request.headers.getlist("x-csrf-token")
    if (
        len(headers) > 1
        or (headers and headers[0] != csrf)
        or not security_settings(request).verify(
            csrf, request.cookies.get(SESSION_COOKIE, ""), "session", SESSION_SECONDS
        )
    ):
        raise ValueError("Invalid form token")
    return values


def lifecycle(
    request: Request,
    document: UUID,
    version: UUID | None,
    operation: Literal["preview", "publish", "revoke"],
) -> LifecycleReport:
    actor = request_identity(request)
    assert actor is not None
    return run_lifecycle_as_actor(
        request.state.staff_db,
        actor=actor,
        operation=operation,
        document_id=document,
        version_id=version,
        pipeline=FilePipeline.from_environment(),
    )


class DocumentsView(StaffBaseView):
    name = "Documents"
    required_role = "admin"

    @expose("/documents", methods=["GET"], identity="documents")
    async def page(self, request: Request) -> Response:
        try:
            start = offset(request)
        except ValueError:
            return error_response(request, 422, "INVALID_REQUEST", "Invalid pagination")

        def read() -> list[object]:
            rows: list[object] = request.state.staff_db.execute(
                select(Document.id, Document.code, Document.status, Document.visibility)
                .order_by(Document.code, Document.id)
                .offset(start)
                .limit(PAGE_SIZE + 1)
            ).all()
            return rows

        try:
            rows = await run_in_threadpool(read)
        except SQLAlchemyError:
            return error_response(
                request, 503, "SERVICE_UNAVAILABLE", "Document list unavailable"
            )
        return await self.templates.TemplateResponse(
            request,
            "content/list.html",
            {"rows": rows, "offset": start, "page_size": PAGE_SIZE},
        )


class DocumentLifecycleView(StaffBaseView):
    name = "Document lifecycle"
    required_role = "admin"

    def is_visible(self, request: Request) -> bool:
        return False

    @expose("/documents/{document}", methods=["GET"], identity="document-detail")
    async def page(self, request: Request) -> Response:
        try:
            document = parse_uuid(request.path_params["document"])
            start = offset(request)
        except ValueError:
            return error_response(request, 422, "INVALID_REQUEST", "Invalid request")

        def read() -> dict[str, object]:
            session = request.state.staff_db
            row = session.execute(
                select(
                    Document.id, Document.code, Document.status, Document.visibility
                ).where(Document.id == document)
            ).one_or_none()
            versions = session.execute(
                select(
                    DocumentVersion.id,
                    DocumentVersion.version,
                    DocumentVersion.status,
                    DocumentVersion.reviewed_by,
                    DocumentVersion.approved_at,
                    DocumentVersion.published_at,
                )
                .where(DocumentVersion.document_id == document)
                .order_by(DocumentVersion.version.desc(), DocumentVersion.id)
                .offset(start)
                .limit(PAGE_SIZE + 1)
            ).all()
            return {"row": row, "versions": versions}

        try:
            context = await run_in_threadpool(read)
        except SQLAlchemyError:
            return error_response(
                request, 503, "SERVICE_UNAVAILABLE", "Document details unavailable"
            )
        if context["row"] is None:
            return error_response(request, 404, "NOT_FOUND", "Document not found")
        return await self.templates.TemplateResponse(
            request,
            "content/detail.html",
            {**context, "offset": start, "page_size": PAGE_SIZE},
        )

    @expose(
        "/documents/{document}/preview", methods=["POST"], identity="document-preview"
    )
    async def preview(self, request: Request) -> Response:
        try:
            document = parse_uuid(request.path_params["document"])
            fields = await action_fields(request, {"operation", "version_id"})
            raw_operation = fields.get("operation")
            if raw_operation not in ("publish", "revoke"):
                raise ValueError("Invalid operation")
            operation = cast(Action, raw_operation)
            version = (
                parse_uuid(fields["version_id"]) if "version_id" in fields else None
            )
            if operation == "publish" and version is None:
                raise ValueError("Version required")
            report = await run_in_threadpool(
                lifecycle, request, document, version, "preview"
            )
        except FileRejected as error:
            return self.refusal(request, LifecycleReport(error.code))
        except (ValueError, HTTPException):
            return error_response(
                request, 422, "INVALID_REQUEST", "Invalid preview form"
            )
        except (SQLAlchemyError, OSError):
            return error_response(
                request, 503, "SERVICE_UNAVAILABLE", "Document preview unavailable"
            )
        if not report.ok:
            return self.refusal(request, report)
        digest = preview_digest(report)
        token = security_settings(request).issue(
            preview_binding(request, document, version, operation, digest), "kb-preview"
        )
        return await self.templates.TemplateResponse(
            request,
            "content/preview.html",
            {
                "document": document,
                "version": version,
                "operation": operation,
                "digest": digest,
                "token": token,
                "dependencies": dependency_summaries(report),
                "status": report.technical["status"],
            },
        )

    @expose(
        "/documents/{document}/versions/{version}/{operation}",
        methods=["POST"],
        identity="document-version-action",
    )
    async def version_action(self, request: Request) -> Response:
        return await self.change(request)

    @expose(
        "/documents/{document}/revoke", methods=["POST"], identity="document-revoke"
    )
    async def document_action(self, request: Request) -> Response:
        return await self.change(request)

    async def change(self, request: Request) -> Response:
        try:
            document = parse_uuid(request.path_params["document"])
            version = (
                parse_uuid(request.path_params["version"])
                if "version" in request.path_params
                else None
            )
            operation = request.path_params.get("operation", "revoke")
            if operation not in ("publish", "revoke"):
                return error_response(request, 404, "NOT_FOUND", "Operation not found")
            fields = await action_fields(
                request, {"preview_digest", "preview_token", "confirm"}
            )
            digest = fields.get("preview_digest", "")
            if (
                fields.get("confirm") != operation
                or re.fullmatch(r"[0-9a-f]{64}", digest) is None
                or not security_settings(request).verify(
                    fields.get("preview_token", ""),
                    preview_binding(request, document, version, operation, digest),
                    "kb-preview",
                    PREVIEW_SECONDS,
                )
            ):
                raise ValueError("Preview confirmation required")

            def change() -> LifecycleReport | None:
                current = lifecycle(request, document, version, "preview")
                if not current.ok:
                    return current
                if preview_digest(current) != digest and not already_applied(
                    current, operation, version
                ):
                    return None
                # Preview retains locks in the same outer staff UoW; execution
                # independently rechecks review, ACL, file and dependencies.
                return lifecycle(request, document, version, operation)

            report = await run_in_threadpool(change)
        except FileRejected as error:
            return self.refusal(request, LifecycleReport(error.code))
        except (ValueError, HTTPException):
            return error_response(
                request,
                422,
                "INVALID_REQUEST",
                "Valid preview and confirmation required",
            )
        except (SQLAlchemyError, OSError):
            return error_response(
                request, 503, "SERVICE_UNAVAILABLE", "Document action unavailable"
            )
        if report is None:
            return error_response(
                request,
                409,
                "PREVIEW_CHANGED",
                "State changed; request a fresh preview",
            )
        if not report.ok:
            return self.refusal(request, report)
        return await self.templates.TemplateResponse(
            request,
            "content/result.html",
            {"code": report.code, "document": document},
        )

    @staticmethod
    def refusal(request: Request, report: LifecycleReport) -> Response:
        return error_response(
            request,
            404 if report.code == "NOT_FOUND" else 409,
            report.code,
            "Lifecycle action rejected; inspect current state and operator evidence",
        )
