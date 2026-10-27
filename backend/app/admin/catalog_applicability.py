"""Explicit expert review and separate disclosure permission in SQLAdmin."""

from typing import cast

from fastapi import Request
from pydantic import ValidationError
from sqladmin import expose
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool
from starlette.responses import RedirectResponse, Response

from app.admin.forms import revision, unique_fields
from app.admin.security import error_response, request_identity
from app.admin.sqladmin import StaffBaseView
from app.modules.catalog.applicability_evidence import (
    ApplicabilityPublication,
    ApplicabilityReview,
)
from app.modules.catalog.applicability_write import ApplicabilityWriteService, Operation
from app.modules.catalog.models import CatalogItem, CatalogItemModel, EquipmentModel
from app.modules.kb.lifecycle_service import LifecycleRejected
from app.modules.kb.models import Document, DocumentSection, DocumentVersion
from app.schemas.common import parse_uuid


class CatalogApplicabilityView(StaffBaseView):
    name = "Applicability"

    def is_visible(self, request: Request) -> bool:
        return False

    @expose(
        "/catalog/items/{record}/applicability",
        methods=["GET"],
        identity="catalog-applicability",
    )
    async def page(self, request: Request) -> Response:
        try:
            identifier = parse_uuid(request.path_params["record"])
        except ValueError:
            return error_response(request, 422, "INVALID_REQUEST", "Invalid identifier")

        def read() -> dict[str, object]:
            session = request.state.staff_db
            row = session.get(CatalogItem, identifier)
            links = session.execute(
                select(CatalogItemModel, EquipmentModel, Document, DocumentVersion)
                .join(EquipmentModel, EquipmentModel.id == CatalogItemModel.model_id)
                .join(
                    DocumentSection,
                    DocumentSection.id == CatalogItemModel.evidence_section_id,
                )
                .join(
                    DocumentVersion,
                    DocumentVersion.id == DocumentSection.document_version_id,
                )
                .join(Document, Document.id == DocumentVersion.document_id)
                .where(CatalogItemModel.item_id == identifier)
                .order_by(EquipmentModel.code)
            ).all()
            actor = request_identity(request)
            assert actor is not None
            service = ApplicabilityWriteService(session, actor)
            reviews = {}
            for link, _, _, _ in links:
                try:
                    reviews[link.model_id] = service._review(link).id
                except LifecycleRejected:
                    pass
            return {"row": row, "links": links, "reviews": reviews}

        context = await run_in_threadpool(read)
        if context["row"] is None:
            return error_response(request, 404, "NOT_FOUND", "Object not found")
        return await self.templates.TemplateResponse(
            request, "catalog/applicability.html", context
        )

    @expose(
        "/catalog/items/{record}/applicability/{operation}",
        methods=["POST"],
        identity="catalog-applicability-action",
    )
    async def action(self, request: Request) -> Response:
        operation = request.path_params["operation"]
        if operation not in {
            "candidate",
            "replace",
            "review",
            "publish",
            "revoke",
            "preview",
        }:
            return error_response(request, 404, "NOT_FOUND", "Operation not found")
        try:
            identifier = parse_uuid(request.path_params["record"])
            allowed = {"csrf_token", "expected_revision", "model_id"}
            if operation in {"candidate", "replace"}:
                allowed.add("evidence_section_id")
            if operation in {"review", "publish"}:
                allowed.update(ApplicabilityReview.model_fields)
                # Item comes from the URL, never from a hidden actor/binding.
                allowed.remove("item_id")
            if operation == "publish":
                allowed.update(
                    {
                        "review_event_id",
                        "fact_publication_allowed",
                        "publication_reference",
                    }
                )
            async with request.form(
                max_files=0, max_fields=16, max_part_size=1024
            ) as form:
                values = unique_fields(form, allowed)
            values.pop("csrf_token", None)
            expected = revision(values.pop("expected_revision", ""))
            model = parse_uuid(values.pop("model_id", ""))
            section = None
            evidence = None
            if operation in {"candidate", "replace"}:
                section = parse_uuid(values.pop("evidence_section_id", ""))
            elif operation in {"review", "publish"}:
                raw: dict[str, object] = {
                    **values,
                    "item_id": str(identifier),
                    "model_id": str(model),
                }
                for flag in ("applicability_checked", "fact_publication_allowed"):
                    if flag in raw:
                        if raw[flag] not in ("true", "false"):
                            raise ValueError("Invalid flag")
                        raw[flag] = raw[flag] == "true"
                schema = (
                    ApplicabilityPublication
                    if operation == "publish"
                    else ApplicabilityReview
                )
                evidence = schema.model_validate(raw)
                values.clear()
            if values:
                raise ValueError("Unexpected fields")
            actor = request_identity(request)
            assert actor is not None

            def change() -> object:
                session = request.state.staff_db
                with session.begin_nested():
                    return ApplicabilityWriteService(session, actor).execute(
                        cast(Operation, operation),
                        identifier,
                        model,
                        section_id=section,
                        evidence=evidence,
                        expected_revision=expected,
                    )

            report = await run_in_threadpool(change)
        except LifecycleRejected as error:
            return error_response(
                request,
                409,
                error.code,
                "Applicability action rejected; reload current state",
            )
        except (ValueError, ValidationError):
            return error_response(
                request, 422, "INVALID_REQUEST", "Invalid applicability fields"
            )
        except SQLAlchemyError:
            return error_response(
                request, 503, "SERVICE_UNAVAILABLE", "Applicability change unavailable"
            )
        if operation == "preview":
            return await self.templates.TemplateResponse(
                request,
                "catalog/applicability_preview.html",
                {"report": report, "identifier": identifier},
            )
        return RedirectResponse(
            f"/admin/catalog/items/{identifier}/applicability", status_code=303
        )
