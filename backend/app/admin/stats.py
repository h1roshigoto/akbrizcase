"""SQLAdmin statistics under the existing staff authority and outer UoW."""

from fastapi import Request
from sqladmin import expose
from sqlalchemy import select, true
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool
from starlette.responses import Response

from app.admin.security import error_response
from app.admin.sqladmin import StaffBaseView
from app.modules.chat.stats_service import feedback_statistics, session_statistics
from app.modules.orders.stats_service import order_statistics
from app.schemas.admin_stats import (
    AdminStats,
    FeedbackStats,
    OrderStats,
    SessionStats,
    StatsPeriod,
)


def read_statistics(session: Session, period: StatsPeriod) -> AdminStats:
    orders = order_statistics(period)
    sessions = session_statistics(period)
    feedback = feedback_statistics(period)
    # One statement = one PostgreSQL MVCC snapshot even under READ COMMITTED.
    # Each subquery returns exactly one aggregate row, including empty periods.
    # No extra transaction, isolation changes, row locks, writes or commits.
    with session.no_autoflush:
        row = session.execute(
            select(*orders.c, *sessions.c, *feedback.c).select_from(
                orders.join(sessions, true()).join(feedback, true())
            )
        ).one()
    return AdminStats(
        period=period,
        orders=OrderStats.model_validate({c.key: row._mapping[c] for c in orders.c}),
        sessions=SessionStats.model_validate(
            {c.key: row._mapping[c] for c in sessions.c}
        ),
        feedback=FeedbackStats.model_validate(
            {c.key: row._mapping[c] for c in feedback.c}
        ),
    )


class StatsView(StaffBaseView):
    name = "Statistics"
    required_role = "manager"

    @expose("/stats", methods=["GET"], identity="stats")
    async def stats_page(self, request: Request) -> Response:
        try:
            period = StatsPeriod.from_query(list(request.query_params.multi_items()))
        except ValueError:
            return error_response(
                request, 422, "INVALID_REQUEST", "Invalid statistics period"
            )
        try:
            stats = await run_in_threadpool(
                read_statistics, request.state.staff_db, period
            )
        except SQLAlchemyError:
            # Handle before SQLAdmin starts its mounted error response. Never
            # interpolate the exception or substitute successful zero counts.
            return error_response(
                request, 503, "SERVICE_UNAVAILABLE", "Statistics unavailable"
            )
        return await self.templates.TemplateResponse(
            request, "stats.html", {"stats": stats}
        )
