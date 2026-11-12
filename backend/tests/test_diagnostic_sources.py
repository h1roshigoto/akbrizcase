"""Combined public link/action reads must fail closed and never retain results."""

from unittest.mock import Mock
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from test_mode_router import source

from app.modules.chat.actions import ActionValidator
from app.modules.kb.access import PUBLIC_VIEWER
from app.modules.kb.read_service import KnowledgeReadService, SourceNotFound


def test_sources_and_actions_share_one_fresh_metadata_projection() -> None:
    current = source()
    metadata = current.model_dump(exclude={"url"})
    session = Mock(spec=Session)
    session.execute.return_value.mappings.return_value = [metadata]
    validator = ActionValidator(session)
    refs, actions = validator.read_documents((current.section_id, current.section_id))
    assert refs == [current, current]
    assert [a.payload for a in actions] == refs
    assert session.execute.call_count == 1
    statement, parameters = session.execute.call_args.args
    assert parameters == {"source_ids": (current.section_id, current.section_id)}
    sql = str(statement.compile(dialect=create_engine("postgresql+psycopg://").dialect))
    assert "document_sections.text" not in sql and "search_vector" not in sql
    assert "assistant_allowed IS true" in sql and "documents.status" in sql
    assert "documents.visibility" in sql and "document_versions.status" in sql
    # The same SQL shape must execute again and see revocation/missing rows.
    session.execute.return_value.mappings.return_value = []
    with pytest.raises(SourceNotFound):
        validator.read_documents((current.section_id,))
    assert session.execute.call_count == 2


def test_missing_dependency_cannot_produce_partial_sources_or_actions() -> None:
    current = source()
    session = Mock(spec=Session)
    session.execute.return_value.mappings.return_value = [
        current.model_dump(exclude={"url"})
    ]
    with pytest.raises(SourceNotFound):
        ActionValidator(session).read_documents((current.section_id, uuid4()))
    session.execute.assert_called_once()


def test_empty_sources_do_not_acquire_a_connection() -> None:
    session = Mock(spec=Session)
    assert KnowledgeReadService(session, PUBLIC_VIEWER).get_sources(()) == []
    assert ActionValidator(session).read_documents(()) == ([], [])
    session.execute.assert_not_called()
