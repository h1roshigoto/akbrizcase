"""Administrative status DTOs, independent of staff auth and persistence."""

from typing import Literal

from pydantic import model_validator

from app.schemas.common import DTO, Comment, OrderStatus, PositiveInt


class OrderStatusChange(DTO):
    status: Literal["in_progress", "closed", "cancelled"]
    expected_version: PositiveInt
    reason: Comment | None = None

    @model_validator(mode="after")
    def closing_reason(self) -> "OrderStatusChange":
        if self.status in ("closed", "cancelled") and self.reason is None:
            raise ValueError("closing or cancelling requires a reason")
        return self


class OrderStatusResult(DTO):
    status: OrderStatus
    version: PositiveInt
