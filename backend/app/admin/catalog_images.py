"""SQLAdmin image adapter; preparation is owned by the narrow image boundary."""

import hashlib
import os

from fastapi import Request
from sqladmin import expose
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool
from starlette.responses import RedirectResponse, Response

from app.admin.catalog import CatalogEditorView, form_values
from app.admin.forms import revision
from app.admin.security import error_response, request_identity
from app.admin.sqladmin import StaffBaseView
from app.modules.catalog.image_service import CatalogImageService
from app.modules.catalog.images.pipeline import ImagePipeline
from app.modules.catalog.models import CatalogItem
from app.modules.catalog.write_service import CatalogRejected
from app.modules.kb.files import FileRejected
from app.schemas.common import parse_uuid


class CatalogImagesView(StaffBaseView):
    name = "Catalog image"

    def is_visible(self, request: Request) -> bool:
        return False

    @expose("/catalog/items/{record}/image", methods=["GET"], identity="catalog-image")
    async def image(self, request: Request) -> Response:
        try:
            identifier = parse_uuid(request.path_params["record"])
        except ValueError:
            return error_response(request, 422, "INVALID_REQUEST", "Invalid identifier")
        row = await run_in_threadpool(
            request.state.staff_db.get, CatalogItem, identifier
        )
        if row is None:
            return error_response(request, 404, "NOT_FOUND", "Object not found")
        return await self.templates.TemplateResponse(
            request, "catalog/image.html", {"row": row}
        )

    @expose(
        "/catalog/items/{record}/image/preview",
        methods=["GET"],
        identity="catalog-image-preview",
    )
    async def preview(self, request: Request) -> Response:
        key = getattr(request.state, "image_preview_key", None)
        if key is None:
            return error_response(request, 404, "NOT_FOUND", "Image unavailable")

        def read() -> Response:
            actor = request_identity(request)
            assert actor is not None
            session = request.state.staff_db
            pipeline = ImagePipeline.from_environment()
            with pipeline.prepare(key) as (metadata, descriptor):
                os.lseek(descriptor, 0, os.SEEK_SET)
                data = os.read(descriptor, metadata.size_bytes + 1)
            if len(data) != metadata.size_bytes or (
                hashlib.sha256(data).hexdigest() != metadata.checksum_sha256
            ):
                raise FileRejected("FILE_CHANGED")
            # No SQL until AV/decode/integrity finish. Recheck current authority
            # and binding before committing the protected response.
            CatalogImageService(session, actor, pipeline)._authorize()
            identifier = parse_uuid(request.path_params["record"])
            current = session.scalar(
                select(CatalogItem.image_key).where(CatalogItem.id == identifier)
            )
            if current != key:
                raise FileRejected("FILE_CHANGED")
            return Response(
                data,
                media_type=metadata.mime,
                headers={"Content-Disposition": "inline", "Cache-Control": "no-store"},
            )

        try:
            return await run_in_threadpool(read)
        except CatalogRejected as error:
            return await CatalogEditorView._rejected(self, request, error)
        except (FileRejected, OSError):
            return error_response(request, 404, "NOT_FOUND", "Image unavailable")
        except SQLAlchemyError:
            return error_response(
                request, 503, "SERVICE_UNAVAILABLE", "Image unavailable"
            )

    @expose(
        "/catalog/items/{record}/image/replace",
        methods=["POST"],
        identity="catalog-image-replace",
    )
    async def replace(self, request: Request) -> Response:
        # The middleware must have authenticated, bounded, parsed and CSRF-checked
        # this exact route before granting the two-stage transaction.
        upload = getattr(request.state, "image_upload", None)
        if upload is None:
            return error_response(request, 422, "INVALID_REQUEST", "Invalid image form")
        try:
            identifier = parse_uuid(request.path_params["record"])
            actor = request_identity(request)
            assert actor is not None
            service = CatalogImageService(
                request.state.staff_db, actor, ImagePipeline.from_environment()
            )
            await run_in_threadpool(
                service.replace,
                identifier,
                request.state.image_revision,
                upload.file,
                filename=upload.filename or "",
                mime=upload.content_type or "",
                declared_size=upload.size,
            )
        except (ValueError, FileRejected):
            return error_response(
                request,
                422,
                "IMAGE_REJECTED",
                "Image rejected; previous image preserved",
            )
        except CatalogRejected as error:
            return await CatalogEditorView._rejected(self, request, error)
        except (SQLAlchemyError, OSError):
            return error_response(
                request, 503, "SERVICE_UNAVAILABLE", "Image change unavailable"
            )
        return RedirectResponse(
            f"/admin/catalog/items/{identifier}/image", status_code=303
        )

    @expose(
        "/catalog/items/{record}/image/remove",
        methods=["POST"],
        identity="catalog-image-remove",
    )
    async def remove(self, request: Request) -> Response:
        try:
            identifier = parse_uuid(request.path_params["record"])
            values = await form_values(request)
            if set(values) != {"expected_revision"}:
                raise ValueError("Unexpected fields")
            expected = revision(str(values["expected_revision"]))
            actor = request_identity(request)
            assert actor is not None
            await run_in_threadpool(
                CatalogImageService(
                    request.state.staff_db, actor, ImagePipeline.from_environment()
                ).remove,
                identifier,
                expected,
            )
        except ValueError:
            return error_response(request, 422, "INVALID_REQUEST", "Invalid image form")
        except CatalogRejected as error:
            return await CatalogEditorView._rejected(self, request, error)
        except (SQLAlchemyError, OSError):
            return error_response(
                request, 503, "SERVICE_UNAVAILABLE", "Image change unavailable"
            )
        return RedirectResponse(
            f"/admin/catalog/items/{identifier}/image", status_code=303
        )
