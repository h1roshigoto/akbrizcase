"""Explicit, authenticated, fresh-target-only developer catalog import.

No HTTP registration, source connection, SQL dump execution or receipt copying.
The outer UoW alone commits catalog/model bindings and the local audit receipt.
Failed file preparation can leave private unbound objects, never published rows.
"""

import hashlib
import io
import json
import os
import re
import stat
import sys
from contextlib import ExitStack
from dataclasses import dataclass
from getpass import GetPassWarning
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, StrictInt, ValidationError, field_validator
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.auth import has_role
from app.core.cli import SafeParser, hidden
from app.core.db import SCHEMA_REVISION, Database, DatabaseSettings
from app.core.release import FullReleaseModes, release_modes, unique
from app.core.staff import authenticate_operator
from app.core.staff_models import AuditEvent
from app.modules.catalog.images.pipeline import MAX_BYTES, ImagePipeline, signature
from app.modules.catalog.models import CatalogItem, EquipmentModel
from app.modules.catalog.write_dto import ItemCreate, ModelFields
from app.modules.kb.files import FileRejected
from app.schemas.common import DTO

SHA = r"[0-9a-f]{64}"
IMAGE = r"images/[0-9a-f-]{36}\.(?:jpg|png)"
Status = Literal["draft", "published", "archived"]
Count = Annotated[StrictInt, Field(ge=0, le=1000)]
ACTION = "catalog.developer_import"


class PackRefused(ValueError):
    """Closed technical code, never caller content or operational details."""


class Counts(DTO):
    catalog_items: Count
    images: Count
    equipment_models: Count


class Provenance(DTO):
    source_code_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    selective_export_sha256: str = Field(pattern="^" + SHA + "$")
    source_report_sha256: str = Field(pattern="^" + SHA + "$")
    authorization: Literal["owner-authorized-private-development"]
    authorization_reference: str = Field(pattern=r"^[A-Z0-9-]{3,100}$")


class Manifest(DTO):
    format: Literal[1]
    classification: Literal["authorized-development/non-production"]
    pack_id: UUID
    schema_revision: Literal["0011_mvp039_http_security"]
    provenance: Provenance
    counts: Counts
    files: dict[str, str] = Field(max_length=1001)

    @field_validator("format", mode="before")
    @classmethod
    def exact_format(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("format")
        return value


class Item(ItemCreate):
    id: UUID
    status: Status
    revision: Annotated[StrictInt, Field(ge=1, le=2147483647)]
    image_file: str | None = Field(pattern="^" + IMAGE + "$")

    @field_validator("price", mode="before")
    @classmethod
    def exact_money(cls, value: object) -> object:
        # JSON money is always a decimal string or null, never binary float.
        if value is not None and (
            not isinstance(value, str)
            or re.fullmatch(r"(?:0|[1-9][0-9]{0,9})\.[0-9]{2}", value) is None
        ):
            raise ValueError("decimal string required")
        return value


class Model(ModelFields):
    id: UUID
    status: Status


class Catalog(DTO):
    catalog_items: list[Item] = Field(min_length=1, max_length=1000)
    equipment_models: list[Model] = Field(max_length=1000)
    # No dependency import exists in this format. Never fabricate evidence.
    catalog_item_models: list[object] = Field(max_length=0)


@dataclass(frozen=True, repr=False)
class Pack:
    manifest: Manifest
    catalog: Catalog
    images: dict[str, bytes]
    digest: str


def _read(directory: int, name: str, limit: int) -> bytes:
    descriptor = os.open(
        name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
    )
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or not 0 < info.st_size <= limit
        ):
            raise PackRefused("PACK_FILE")
        data = stream.read(limit + 1)
        if len(data) != info.st_size:
            raise PackRefused("PACK_FILE_CHANGED")
        return data


def _json(data: bytes) -> object:
    return json.loads(
        data,
        object_pairs_hook=unique,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
    )


def _display_guard(value: object) -> None:
    """Reject auth/customer payloads nested inside the display attributes."""
    if isinstance(value, dict):
        for key, child in value.items():
            if re.search(
                r"password|secret|token|credential|session|staff|orders?|customer|"
                r"contact|smtp|private.key|audit|outbox",
                str(key),
                re.I,
            ):
                raise PackRefused("FORBIDDEN_ATTRIBUTE")
            _display_guard(child)
    elif isinstance(value, list):
        for child in value:
            _display_guard(child)


def load_pack(root: Path) -> Pack:
    """Read a bounded allowlisted directory into a checked in-memory snapshot.

    Archives are deliberately unsupported. Callers must never extract a received
    tar/zip over storage. No paths from the manifest are opened as caller paths.
    """
    try:
        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        with ExitStack() as stack:
            stack.callback(os.close, directory)
            if set(os.listdir(directory)) != {
                "manifest.json",
                "catalog.json",
                "images",
            }:
                raise PackRefused("PACK_FILES")
            manifest_bytes = _read(directory, "manifest.json", 256 * 1024)
            _json(manifest_bytes)
            manifest = Manifest.model_validate_json(manifest_bytes)
            catalog_bytes = _read(directory, "catalog.json", 4 * 1024**2)
            _json(catalog_bytes)
            catalog = Catalog.model_validate_json(catalog_bytes)
            files = {"catalog.json": catalog_bytes}
            images_fd = os.open(
                "images",
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=directory,
            )
            stack.callback(os.close, images_fd)
            expected_images = {
                item.image_file for item in catalog.catalog_items if item.image_file
            }
            if set(manifest.files) != {"catalog.json", *expected_images}:
                raise PackRefused("PACK_FILES")
            if set(os.listdir(images_fd)) != {p.split("/")[1] for p in expected_images}:
                raise PackRefused("PACK_FILES")
            total = 0
            for path in sorted(expected_images):
                data = _read(images_fd, path.split("/")[1], MAX_BYTES)
                total += len(data)
                if total > 64 * 1024**2:
                    raise PackRefused("PACK_SIZE")
                expected_mime = "image/png" if path.endswith(".png") else "image/jpeg"
                if signature(data) != expected_mime:
                    raise PackRefused("IMAGE_FORMAT")
                files[path] = data
            for path, data in files.items():
                checksum = manifest.files[path]
                if (
                    re.fullmatch(SHA, checksum) is None
                    or hashlib.sha256(data).hexdigest() != checksum
                ):
                    raise PackRefused("PACK_CHECKSUM")
            items, models = catalog.catalog_items, catalog.equipment_models
            for rows, code in ((items, "sku"), (models, "code")):
                if len({row.id for row in rows}) != len(rows) or len(
                    {getattr(row, code) for row in rows}
                ) != len(rows):
                    raise PackRefused("PACK_DUPLICATE")
                if any(row.id.version != 4 for row in rows):
                    raise PackRefused("PACK_UUID")
            if manifest.pack_id.version != 4:
                raise PackRefused("PACK_UUID")
            if manifest.counts != Counts(
                catalog_items=len(items),
                equipment_models=len(models),
                images=len(expected_images),
            ):
                raise PackRefused("PACK_COUNTS")
            for item in items:
                if item.image_file and Path(item.image_file).stem != str(item.id):
                    raise PackRefused("IMAGE_BINDING")
                _display_guard(item.attributes)
            return Pack(
                manifest,
                catalog,
                {p: files[p] for p in expected_images},
                hashlib.sha256(manifest_bytes).hexdigest(),
            )
    except (
        OSError,
        ValidationError,
        UnicodeError,
        ValueError,
        RecursionError,
        FileRejected,
    ) as error:
        if isinstance(error, PackRefused):
            raise
        raise PackRefused("PACK_INVALID") from None


def _environment(project: str) -> None:
    # The checked controller witness binds the real namespace, DB hostname,
    # profile and owner-only database marker; an env flag alone is insufficient.
    if (
        not re.fullmatch(r"akbriz-staging-[0-9a-f]{12}", project)
        or project != os.environ.get("RELEASE_PROJECT")
        or not isinstance(release_modes(), FullReleaseModes)
    ):
        raise PackRefused("TARGET_ENVIRONMENT")


def _target(session: Session) -> None:
    if (
        session.scalar(
            text(
                "SELECT shobj_description(oid,'pg_database') FROM pg_database "
                "WHERE datname=current_database()"
            )
        )
        != "akbrizcase:synthetic-seed:demo"
    ):
        raise PackRefused("TARGET_DATABASE")
    if set(session.scalars(text("SELECT version_num FROM app.alembic_version"))) != {
        SCHEMA_REVISION
    }:
        raise PackRefused("TARGET_SCHEMA")


def _authorize(session: Session, login: str, password: str) -> UUID:
    actor = authenticate_operator(session, login, password)
    if actor is None or not has_role(actor.user, "admin"):
        raise PackRefused("ADMIN_REQUIRED")
    return actor.user.id


def _state(session: Session, pack: Pack) -> dict[str, str] | None:
    receipts = list(
        session.scalars(select(AuditEvent).where(AuditEvent.action == ACTION))
    )
    items = list(session.scalars(select(CatalogItem)))
    models = list(session.scalars(select(EquipmentModel)))
    if not receipts:
        # Staff-bootstrap is the only permitted business initialization. All
        # source staff/auth/history/KB/orders remain absent from the pack.
        if items or models:
            raise PackRefused("TARGET_NOT_EMPTY")
        for table in (
            "orders",
            "documents",
            "chat_sessions",
            "catalog_item_models",
            "consent_texts",
            "diag_trees",
        ):
            if session.scalar(text(f"SELECT count(*) FROM app.{table}")):
                raise PackRefused("TARGET_NOT_EMPTY")
        if session.scalar(text("SELECT count(*) FROM app.staff_users")) != 1:
            raise PackRefused("TARGET_NOT_FRESH")
        return None
    if (
        len(receipts) != 1
        or receipts[0].entity_id != pack.manifest.pack_id
        or receipts[0].details.get("pack_sha256") != pack.digest
    ):
        raise PackRefused("TARGET_CONFLICT")
    raw_images = receipts[0].details.get("image_sha256")
    if not isinstance(raw_images, str):
        raise PackRefused("TARGET_CONFLICT")
    try:
        receipt = _json(raw_images.encode())
    except ValueError:
        raise PackRefused("TARGET_CONFLICT") from None
    if not isinstance(receipt, dict) or set(receipt) != set(pack.images):
        raise PackRefused("TARGET_CONFLICT")
    if len(items) != len(pack.catalog.catalog_items) or len(models) != len(
        pack.catalog.equipment_models
    ):
        raise PackRefused("TARGET_CONFLICT")
    by_id = {row.id: row for row in items}
    keys: dict[str, str] = {}
    for item in pack.catalog.catalog_items:
        row = by_id.get(item.id)
        fields = item.model_dump(exclude={"image_file"})
        if row is None or any(
            getattr(row, field) != value for field, value in fields.items()
        ):
            raise PackRefused("TARGET_CONFLICT")
        if item.image_file:
            if not row.image_key:
                raise PackRefused("TARGET_CONFLICT")
            keys[item.image_file] = row.image_key
        elif row.image_key is not None:
            raise PackRefused("TARGET_CONFLICT")
    model_ids = {row.id: row for row in models}
    for model in pack.catalog.equipment_models:
        found = model_ids.get(model.id)
        if (
            found is None
            or found.manual_document_id is not None
            or any(
                getattr(found, key) != value
                for key, value in model.model_dump().items()
            )
        ):
            raise PackRefused("TARGET_CONFLICT")
    if session.scalar(text("SELECT count(*) FROM app.catalog_item_models")):
        raise PackRefused("TARGET_CONFLICT")
    return keys


def import_pack(
    database: Database,
    root: Path,
    *,
    project: str,
    login: str,
    password: str,
    pipeline: ImagePipeline,
) -> dict[str, object]:
    """Authorized narrow contract: preserve identifiers only on a fresh target.

    The host operator must first verify newly created controller-owned volumes
    and mount isolation. Runtime DB credentials confer no admin authorization.
    """
    _environment(project)
    pack = load_pack(root)
    if pipeline.root != Path(
        os.environ.get("APP_FILES_DIR", "/var/lib/akbrizcase/files")
    ):
        raise PackRefused("TARGET_STORAGE")
    if not database.ready():
        raise PackRefused("TARGET_NOT_READY")
    with database.unit_of_work() as session:
        _target(session)
        _authorize(session, login, password)
        existing = _state(session, pack)
    # No DB transaction or staff lock is held across expensive AV/decode work.
    with ExitStack() as files:
        keys: dict[str, str] = {}
        outputs: dict[str, str] = {}
        for path, data in sorted(pack.images.items()):
            if existing is None:
                accepted = pipeline.accept(
                    io.BytesIO(data),
                    filename=path.split("/")[1],
                    mime="image/png" if path.endswith(".png") else "image/jpeg",
                    declared_size=len(data),
                )
                key = accepted.image_key
            else:
                key = existing[path]
            metadata, _ = files.enter_context(pipeline.prepare(key))
            keys[path] = key
            outputs[path] = metadata.checksum_sha256
        _environment(project)
        with database.unit_of_work() as session:
            _target(session)
            # This also serializes with ordinary catalog writes. Files are
            # already prepared; there is no scan while holding database locks.
            actor_id = _authorize(session, login, password)
            session.execute(
                text(
                    "LOCK TABLE app.catalog_items, app.equipment_models, app.orders, "
                    "app.documents, app.chat_sessions, app.catalog_item_models, "
                    "app.consent_texts, app.diag_trees "
                    "IN SHARE ROW EXCLUSIVE MODE"
                )
            )
            current = _state(session, pack)
            if current != existing:
                raise PackRefused("TARGET_CHANGED")
            if current is not None:
                receipt = session.scalars(
                    select(AuditEvent).where(AuditEvent.action == ACTION)
                ).one()
                if receipt.details.get("image_sha256") != json.dumps(
                    outputs, sort_keys=True
                ):
                    raise PackRefused("TARGET_IMAGE_CHANGED")
            else:
                for model in pack.catalog.equipment_models:
                    session.add(EquipmentModel(**model.model_dump()))
                for item in pack.catalog.catalog_items:
                    session.add(
                        CatalogItem(
                            **item.model_dump(exclude={"image_file"}),
                            image_key=keys[item.image_file]
                            if item.image_file
                            else None,
                        )
                    )
                session.add(
                    AuditEvent(
                        actor_id=actor_id,
                        action=ACTION,
                        entity_type="developer_pack",
                        entity_id=pack.manifest.pack_id,
                        result="success",
                        details={
                            "pack_sha256": pack.digest,
                            "image_sha256": json.dumps(outputs, sort_keys=True),
                        },
                    )
                )
                session.flush()
            # Exit integrity checks happen BEFORE the outer UoW commits.
            files.close()
    return {
        "code": "ALREADY_IMPORTED" if existing is not None else "IMPORTED",
        "counts": pack.manifest.counts.model_dump(),
        "source_sha256": {p: pack.manifest.files[p] for p in pack.images},
        "served_sha256": outputs,
    }


def main(argv: list[str] | None = None) -> int:
    parser = SafeParser(description=__doc__)
    parser.add_argument("operation", choices=("validate", "import"))
    parser.add_argument("--pack", type=Path, required=True)
    parser.add_argument("--target-project")
    args = parser.parse_args(argv)
    database = None
    try:
        if args.operation == "validate":
            pack = load_pack(args.pack)
            print(
                json.dumps(
                    {"code": "PACK_VALID", "counts": pack.manifest.counts.model_dump()}
                )
            )
        else:
            _environment(args.target_project or "")
            # Validation and target authorization precede every storage write.
            load_pack(args.pack)
            login, password = hidden("Synthetic admin login: "), hidden("Password: ")
            database = Database(DatabaseSettings.from_environment())
            result = import_pack(
                database,
                args.pack,
                project=args.target_project,
                login=login,
                password=password,
                pipeline=ImagePipeline.from_environment(),
            )
            print(json.dumps(result, sort_keys=True))
        return 0
    except PackRefused as error:
        print(str(error), file=sys.stderr)
    except (
        OSError,
        ValueError,
        SQLAlchemyError,
        FileRejected,
        GetPassWarning,
        EOFError,
        KeyboardInterrupt,
    ):
        print("PACK_OPERATION_FAILED_NO_SUCCESS_RECEIPT", file=sys.stderr)
    finally:
        if database is not None:
            database.close()
    return 1


if __name__ == "__main__":
    sys.exit(main())
