"""Read-only, bounded SQL aggregate for orders surviving at query time."""

from sqlalchemy import func, select
from sqlalchemy.sql.selectable import Subquery

from app.modules.orders.models import Order
from app.schemas.admin_stats import StatsPeriod


def order_statistics(period: StatsPeriod) -> Subquery:
    return (
        select(
            func.count().label("total"),
            *(
                func.count().filter(Order.kind == kind).label(kind)
                for kind in ("commercial", "support")
            ),
            *(
                func.count().filter(Order.status == status).label(status)
                for status in ("new", "in_progress", "closed", "cancelled")
            ),
            *(
                func.count().filter(Order.source == source).label(source)
                for source in ("site", "chat")
            ),
            func.count()
            .filter(Order.chat_session_id.is_not(None))
            .label("chat_linked"),
        )
        .select_from(Order)
        .where(Order.created_at >= period.start_utc, Order.created_at < period.stop_utc)
        .subquery("order_statistics")
    )
