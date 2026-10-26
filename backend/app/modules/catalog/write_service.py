"""Admin-only catalog mutations under the same row locks as order creation."""

import json
from datetime import datetime, timedelta
from typing import Literal, NoReturn
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.auth import has_role
from app.core.staff_models import AuditEvent
from app.core.staff_sessions import StaffIdentity
from app.modules.catalog.models import CatalogItem, EquipmentModel
from app.modules.catalog.write_dto import (
    ItemCreate,
    ItemEdit,
    ItemFields,
    ItemState,
    ModelEdit,
    ModelFields,
    ModelState,
    StateAction,
)
from app.schemas.common import DTO


def command_values(command: DTO) -> dict[str, object]:
    # Money's wire serializer formats to two decimals even in model_dump().
    # Validate and compare original typed values; never round before validation.
    return {key: getattr(command, key) for key in type(command).model_fields}


class CatalogRejected(Exception):
    def __init__(self, status: int, message: str) -> None:
        self.status = status
        self.message = message
        super().__init__(message)


class CatalogWriteService:
    def __init__(self, session: Session, actor: StaffIdentity) -> None:
        if not has_role(actor.user, "admin"):
            raise CatalogRejected(403, "Insufficient permissions")
        self.session = session
        self.actor_id = actor.user.id

    def _audit(
        self,
        row: CatalogItem | EquipmentModel,
        action: Literal["create", "edit", "publish", "archive"],
    ) -> None:
        entity = "catalog_item" if isinstance(row, CatalogItem) else "equipment_model"
        details: dict[str, str | bool | int] = {"status": row.status}
        if isinstance(row, CatalogItem):
            details["revision"] = row.revision
        self.session.add(
            AuditEvent(
                actor_id=self.actor_id,
                action=f"{entity}.{action}",
                entity_type=entity,
                entity_id=row.id,
                result="success",
                details=details,
            )
        )
        self.session.flush()

    @staticmethod
    def _unique(error: IntegrityError) -> NoReturn:
        original = error.orig
        if getattr(original, "sqlstate", None) == "23505" and getattr(
            getattr(original, "diag", None), "constraint_name", None
        ) in {"uq_catalog_items_sku", "uq_equipment_models_code"}:
            raise CatalogRejected(409, "SKU or model code already exists") from None
        raise error

    def create_item(self, command: ItemCreate) -> CatalogItem:
        command = ItemCreate.model_validate(command_values(command))
        try:
            with self.session.begin_nested():
                row = CatalogItem(**command_values(command), status="draft", revision=1)
                self.session.add(row)
                self.session.flush()
                self._audit(row, "create")
                return row
        except IntegrityError as error:
            self._unique(error)

    def create_model(self, command: ModelFields) -> EquipmentModel:
        command = ModelFields.model_validate(command_values(command))
        try:
            with self.session.begin_nested():
                row = EquipmentModel(**command_values(command), status="draft")
                self.session.add(row)
                self.session.flush()
                self._audit(row, "create")
                return row
        except IntegrityError as error:
            self._unique(error)

    def _item(self, identifier: UUID, expected_revision: int) -> CatalogItem:
        row = self.session.scalar(
            select(CatalogItem)
            .where(CatalogItem.id == identifier)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if row is None:
            raise CatalogRejected(404, "Object not found")
        if row.revision != expected_revision:
            raise CatalogRejected(409, "Catalog changed; reload its current state")
        if row.revision == 2147483647:
            raise CatalogRejected(409, "Revision limit reached")
        return row

    def _model(self, identifier: UUID, expected_updated_at: datetime) -> EquipmentModel:
        row = self.session.scalar(
            select(EquipmentModel)
            .where(EquipmentModel.id == identifier)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if row is None:
            raise CatalogRejected(404, "Object not found")
        if row.updated_at != expected_updated_at:
            raise CatalogRejected(409, "Model changed; reload its current state")
        return row

    def _changed(
        self,
        row: CatalogItem | EquipmentModel,
        action: Literal["edit", "publish", "archive"],
    ) -> None:
        now = self.session.scalar(select(func.clock_timestamp()))
        assert now is not None
        row.updated_at = max(now, row.updated_at + timedelta(microseconds=1))
        if isinstance(row, CatalogItem):
            row.revision += 1
        self.session.flush()
        self._audit(row, action)

    def edit_item(self, identifier: UUID, command: ItemEdit) -> CatalogItem:
        command = ItemEdit.model_validate(command_values(command))
        with self.session.begin_nested():
            row = self._item(identifier, command.expected_revision)
            if row.status == "archived":
                raise CatalogRejected(409, "Archived records are read-only")
            fields = {key: getattr(command, key) for key in ItemFields.model_fields}
            ItemCreate.model_validate({**fields, "sku": row.sku, "kind": row.kind})
            # Python equates True and 1; JSON display data must distinguish them.
            attributes_changed = json.dumps(
                row.attributes, sort_keys=True
            ) != json.dumps(command.attributes, sort_keys=True)
            if attributes_changed or any(
                getattr(row, key) != value
                for key, value in fields.items()
                if key != "attributes"
            ):
                for key, value in fields.items():
                    setattr(row, key, value)
                self._changed(row, "edit")
            return row

    def edit_model(self, identifier: UUID, command: ModelEdit) -> EquipmentModel:
        command = ModelEdit.model_validate(command_values(command))
        try:
            with self.session.begin_nested():
                row = self._model(identifier, command.expected_updated_at)
                if row.status == "archived":
                    raise CatalogRejected(409, "Archived records are read-only")
                fields = {
                    key: getattr(command, key) for key in ModelFields.model_fields
                }
                if any(getattr(row, key) != value for key, value in fields.items()):
                    for key, value in fields.items():
                        setattr(row, key, value)
                    self._changed(row, "edit")
                return row
        except IntegrityError as error:
            self._unique(error)

    @staticmethod
    def _transition(current: str, action: StateAction) -> str:
        if action == "publish" and current == "draft":
            return "published"
        if action == "archive" and current == "published":
            return "archived"
        raise CatalogRejected(409, "Publication transition is not allowed")

    def change_item_state(
        self, identifier: UUID, command: ItemState, action: StateAction
    ) -> CatalogItem:
        command = ItemState.model_validate(command_values(command))
        with self.session.begin_nested():
            row = self._item(identifier, command.expected_revision)
            target = self._transition(row.status, action)
            ItemCreate.model_validate(
                {key: getattr(row, key) for key in ItemCreate.model_fields}
            )
            row.status = target
            self._changed(row, action)
            return row

    def change_model_state(
        self, identifier: UUID, command: ModelState, action: StateAction
    ) -> EquipmentModel:
        command = ModelState.model_validate(command_values(command))
        with self.session.begin_nested():
            row = self._model(identifier, command.expected_updated_at)
            target = self._transition(row.status, action)
            ModelFields.model_validate(
                {key: getattr(row, key) for key in ModelFields.model_fields}
            )
            row.status = target
            self._changed(row, action)
            return row
