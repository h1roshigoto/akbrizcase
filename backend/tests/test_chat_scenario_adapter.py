"""The HTTP adapter uses the public menu and an explicit bounded projection."""

from contextlib import contextmanager
from dataclasses import replace
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from test_diagnosis import tree as tree

from app.main import app
from app.modules.kb.tree_reads import ScenarioChoice, TreeSnapshot, published_scenarios


def test_menu_limit_is_after_public_loader(
    monkeypatch: pytest.MonkeyPatch, tree: TreeSnapshot
) -> None:
    class Keys:
        def all(self) -> list[str]:
            return [str(i) for i in range(25)]

    class Reader:
        def scalars(self, statement: Any) -> Keys:
            return Keys()

    visited = []

    def load(session: Session, *, key: str) -> TreeSnapshot | None:
        visited.append(key)
        return (
            None
            if int(key) < 12
            else replace(tree, content=tree.content.model_copy(update={"key": key}))
        )

    monkeypatch.setattr("app.modules.kb.tree_reads.load_tree", load)
    result = published_scenarios(Reader(), tree.model_id)  # type: ignore[arg-type]
    assert [r.key for r in result] == [str(i) for i in range(12, 22)]
    assert visited == [str(i) for i in range(22)]


def test_http_explicit_projection_and_uow(monkeypatch: pytest.MonkeyPatch) -> None:
    class Database:
        committed = False

        @contextmanager
        def unit_of_work(self):  # type: ignore[no-untyped-def]
            yield object()
            self.committed = True

    database = Database()
    model = UUID(int=1)
    monkeypatch.delenv("DB_PASSWORD_FILE", raising=False)
    monkeypatch.setattr(
        "app.modules.chat.router.begin_projection_read", lambda session: None
    )
    monkeypatch.setattr(
        "app.modules.chat.router.published_scenarios",
        lambda session, identifier: [
            ScenarioChoice("SYNTHETIC", "Сценарий", identifier)
        ],
    )
    with TestClient(app) as browser:
        app.state.database = database
        response = browser.get(
            "/api/v1/chat/scenarios", params={"model_id": str(model)}
        )
        app.state.database = None
    assert database.committed
    assert response.status_code == 200
    assert response.json() == {
        "items": [{"key": "SYNTHETIC", "title": "Сценарий", "model_id": str(model)}]
    }
    assert response.headers["cache-control"] == "no-store"


def test_scenario_database_failure_is_sanitized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sqlalchemy.exc import SQLAlchemyError

    class FailingDatabase:
        @contextmanager
        def unit_of_work(self):  # type: ignore[no-untyped-def]
            raise SQLAlchemyError("SYNTHETIC_PRIVATE_SQL")
            yield

    monkeypatch.delenv("DB_PASSWORD_FILE", raising=False)
    with TestClient(app) as browser:
        app.state.database = FailingDatabase()
        response = browser.get(
            "/api/v1/chat/scenarios", params={"model_id": str(UUID(int=1))}
        )
        app.state.database = None
    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["error"]["code"] == "SERVICE_UNAVAILABLE"
    assert "SYNTHETIC_PRIVATE_SQL" not in response.text
