"""Synthetic linked corpus; real services, AV, UoWs and bounded PG lock probes."""

import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx2 import Client
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_catalog_applicability import Corpus, confirmed, private_json
from test_diagnostic_trees import invoke
from test_kb_lifecycle import draft, live_http, review_publish
from test_tree_lifecycle import run

from app.core.db import Database
from app.core.db_types import utc_now
from app.core.http_security import TrustedProxy
from app.core.rate_limits import IpSecurity
from app.main import app
from app.modules.catalog.models import CatalogItem, EquipmentModel
from app.modules.chat.models import ChatMessage, ChatSession
from app.modules.kb.draft_service import (
    DraftEditService,
    SectionContent,
)
from app.modules.kb.files import FilePipeline
from app.modules.kb.models import DocumentSection
from app.modules.kb.tree_models import DiagnosticTree


@dataclass(frozen=True)
class Linked:
    c: Corpus
    needle: str
    tree: UUID
    key: str
    path: Path
    proof: Path


def source_corpus(
    session: Session,
    pipeline: FilePipeline,
    directory: Path,
    *,
    visibility: str = "public",
    kind: str = "product",
) -> tuple[Corpus, str]:
    from app.core.staff import authenticate_operator

    needle = "synthetic045" + uuid4().hex
    login, document, version = draft(
        session,
        pipeline,
        directory,
        visibility=visibility,
        title="PRIVATE045-title-" + needle,
    )
    from test_kb_import import PASSWORD

    actor = authenticate_operator(session, login, PASSWORD)
    assert actor is not None
    section = session.scalar(
        select(DocumentSection.id).where(DocumentSection.document_version_id == version)
    )
    assert section is not None
    DraftEditService(session, actor).edit_section(
        section,
        SectionContent(
            "SYNTHETIC 045 section", needle + " PRIVATE045-fragment", None, None, True
        ),
    )
    review_publish(
        session,
        pipeline,
        directory,
        login,
        document,
        version,
        public=visibility == "public",
    )
    item = CatalogItem(
        sku="SYNTHETIC045-" + uuid4().hex,
        kind=kind,
        name=needle,
        description="SYNTHETIC ONLY",
        unit="test",
        price_mode="on_request",
        tax_note="SYNTHETIC",
        status="published",
    )
    model = EquipmentModel(
        code="SYNTHETIC045-" + uuid4().hex,
        name="SYNTHETIC ONLY",
        description="SYNTHETIC",
        status="published",
    )
    session.add_all([item, model])
    session.flush()
    section = session.scalar(
        select(DocumentSection.id).where(DocumentSection.document_version_id == version)
    )
    assert section is not None
    return Corpus(
        login, actor.user.id, item.id, model.id, section, version, document
    ), needle


def import_tree(
    session: Session, c: Corpus, directory: Path, value: dict[str, Any]
) -> tuple[UUID, Path, Path]:
    path = directory / (uuid4().hex + ".yaml")
    path.write_text(json.dumps(value))
    report = invoke(session, c, path)
    assert report.ok and report.tree_id, report.code
    preview = run(session, c, report.tree_id, "preview")
    proof = private_json(
        directory,
        {
            "tree_id": str(report.tree_id),
            "yaml_checksum": preview.technical["yaml_checksum"],
            "binding_sha256": preview.technical["binding_sha256"],
            "expert_name": "SYNTHETIC ONLY Expert",
            "review_reference": "SYNTHETIC:045:REVIEW",
            "approved_at": utc_now().isoformat(),
            "decision": "approved",
            "content_checked": True,
        },
    )
    return report.tree_id, path, proof


def linked(
    database: Database,
    pipeline: FilePipeline,
    directory: Path,
    *,
    publish: bool = True,
    foreign_node: bool = False,
) -> Linked:
    from test_diagnostic_trees import payload

    with database.unit_of_work() as session:
        c, needle = source_corpus(session, pipeline, directory)
        confirmed(session, c, directory)
        value = payload(session, c)
        value["nodes"]["observe"]["text"] = needle + " question"
        value["nodes"]["result"]["text"] = needle + " result"
        if foreign_node:
            other = json.loads(json.dumps(value["nodes"]["observe"]))
            other["options"][0]["id"] = "foreign-node-option"
            value["nodes"]["other"] = other
            value["nodes"]["observe"]["options"][0]["next"] = "other"
        tree, path, proof = import_tree(session, c, directory, value)
        if publish:
            assert run(session, c, tree, "publish", path, proof).ok
        return Linked(c, needle, tree, value["key"], path, proof)


@pytest.fixture
def http(database: Database, pipeline: FilePipeline) -> Iterator[Client]:
    app.state.ip_security = IpSecurity.from_environment()
    app.state.trusted_proxy = TrustedProxy.from_environment()
    with live_http(database, pipeline) as browser:
        yield browser


def own(browser: Any, mode: str = "diagnose") -> dict[str, Any]:
    response = browser.post("/api/v1/chat/sessions", json={"mode": mode})
    assert response.status_code == 201, response.text
    result: dict[str, Any] = response.json()
    return result


def request(owner: dict[str, Any], version: int = 0, **input: Any) -> dict[str, Any]:
    return {
        "session_id": owner["session_id"],
        "request_id": str(uuid4()),
        "expected_version": version,
        "input": input,
    }


def send(browser: Any, owner: dict[str, Any], body: dict[str, Any]) -> Any:
    return browser.post(
        "/api/v1/chat/messages",
        json=body,
        headers={"X-Chat-Token": owner["session_token"]},
    )


def history(browser: Any, owner: dict[str, Any]) -> Any:
    return browser.get(
        "/api/v1/chat/sessions/" + owner["session_id"],
        headers={"X-Chat-Token": owner["session_token"]},
    )


def start(owner: dict[str, Any], graph: Linked, version: int = 0) -> dict[str, Any]:
    return request(
        owner,
        version,
        type="start_diagnosis",
        model_id=str(graph.c.model),
        tree_key=graph.key,
    )


def ok(response: Any, resolution: str | None = None) -> dict[str, Any]:
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    result: dict[str, Any] = response.json()
    if resolution:
        assert result["reply"]["resolution"] == resolution
    return result


def no_leak(value: object, *secrets: object) -> None:
    __tracebackhide__ = True
    encoded = json.dumps(value, ensure_ascii=False, default=str)
    assert all(str(secret) not in encoded for secret in secrets), (
        "Hidden data disclosed"
    )


def progress(database: Database, owner: dict[str, Any]) -> tuple[Any, list[Any]]:
    with database.unit_of_work() as session:
        row = session.get(ChatSession, UUID(owner["session_id"]))
        records = list(
            session.scalars(
                select(ChatMessage)
                .where(ChatMessage.session_id == UUID(owner["session_id"]))
                .order_by(ChatMessage.state_version_after)
            )
        )
        assert row is not None
        return row, records


def tree_key(session: Session, tree: UUID) -> str:
    key = session.scalar(select(DiagnosticTree.key).where(DiagnosticTree.id == tree))
    assert key is not None
    return key
