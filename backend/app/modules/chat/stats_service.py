"""Bounded SQL aggregates over current sessions and accepted feedback exchanges."""

from sqlalchemy import func, literal, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql.selectable import Subquery

from app.modules.chat.models import ChatMessage, ChatSession
from app.schemas.admin_stats import StatsPeriod


def session_statistics(period: StatsPeriod) -> Subquery:
    return (
        select(
            func.count().label("total"),
            *(
                func.count().filter(ChatSession.mode == mode).label(mode)
                for mode in ("diagnose", "knowledge", "commerce")
            ),
            *(
                func.count().filter(ChatSession.outcome == outcome).label(outcome)
                for outcome in ("resolved", "handoff", "unresolved")
            ),
            func.count().filter(ChatSession.outcome.is_(None)).label("no_outcome"),
        )
        .select_from(ChatSession)
        .where(
            ChatSession.created_at >= period.start_utc,
            ChatSession.created_at < period.stop_utc,
        )
        .subquery("session_statistics")
    )


def feedback_statistics(period: StatsPeriod) -> Subquery:
    # messages.create_message stores code='ok' non-text inputs as 'button'.
    # Rejected feedback can retain resolved/handoff state, so outcome alone is
    # insufficient. The reply and state must also match the accepted transition.
    # UNIQUE(session_id, request_id) and replay's early return prevent recounting.
    response = ChatMessage.response
    accepted = (
        ChatMessage.input["type"].astext == "feedback",
        ChatMessage.matched_by == "button",
        response["format_version"] == literal(1, type_=JSONB),
        response["reply"]["mode"].astext == "diagnose",
        response["state_after"]["mode"].astext == "diagnose",
    )
    return (
        select(
            func.count()
            .filter(
                *accepted,
                ChatMessage.input["useful"] == literal(True, type_=JSONB),
                response["reply"]["resolution"].astext == "completed",
                response["state_after"]["outcome"].astext == "resolved",
            )
            .label("positive"),
            func.count()
            .filter(
                *accepted,
                ChatMessage.input["useful"] == literal(False, type_=JSONB),
                response["reply"]["resolution"].astext == "handoff",
                response["state_after"]["outcome"].astext == "handoff",
            )
            .label("negative"),
        )
        .select_from(ChatMessage)
        .where(
            ChatMessage.created_at >= period.start_utc,
            ChatMessage.created_at < period.stop_utc,
        )
        .subquery("feedback_statistics")
    )
