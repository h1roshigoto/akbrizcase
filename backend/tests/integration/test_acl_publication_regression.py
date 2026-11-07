"""MVP-045: linked publication/ACL/diagnosis acceptance on isolated PG16.

Synthetic technical claims only. HTTP diagnosis uses actual loopback TCP;
staff cookies use the existing HTTPS ASGI harness. Local module suites retain
their detailed negative file/auth/transaction matrices without duplication.
"""

import hashlib
import io
import json
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from threading import Event
from typing import Any
from uuid import UUID, uuid4

import pytest
from acl_publication_helpers import (
    Linked,
    history,
    import_tree,
    linked,
    no_leak,
    ok,
    own,
    progress,
    request,
    send,
    source_corpus,
    start,
)
from acl_publication_helpers import http as http
from commercial_helpers import pg_fault, wait_for_blocker
from fastapi.testclient import TestClient
from httpx2 import Client, RequestError
from sqlalchemy import event, select, text, update
from sqlalchemy.exc import SQLAlchemyError
from test_catalog_applicability import command, confirmed, proofs, service
from test_chat_messages import browsers as browsers
from test_chat_messages import message_db as message_db
from test_diagnostic_trees import payload
from test_kb_import import PASSWORD, admin
from test_kb_import import pipeline as pipeline
from test_kb_lifecycle import (
    cli_request,
    operation,
    review_publish,
    second_draft,
)
from test_kb_reads import edition
from test_tree_lifecycle import run

from app.core.auth import AuthenticatedStaff
from app.core.csrf import SESSION_COOKIE
from app.core.db import Database
from app.core.db_types import utc_now
from app.core.staff import disable_staff
from app.core.staff_models import AuditEvent, StaffSession, StaffUser
from app.main import app
from app.modules.catalog.applicability_write import ApplicabilityWriteService
from app.modules.catalog.models import CatalogItem, CatalogItemModel, EquipmentModel
from app.modules.chat.message_state import decode_response
from app.modules.chat.mode_router import ModeRouter
from app.modules.kb.access import PUBLIC_VIEWER, cookie_viewer
from app.modules.kb.files import FilePipeline
from app.modules.kb.lifecycle_service import LifecycleService
from app.modules.kb.models import Document
from app.modules.kb.read_service import KnowledgeReadService, SourceNotFound, file_url
from app.modules.kb.router import file_pipeline
from app.modules.kb.tree_models import DiagnosticTree
from app.modules.kb.tree_reads import load_tree
from app.modules.kb.tree_schema import TreeRejected, parse_tree

pytestmark = pytest.mark.integration
ORIGIN = "https://localhost:8443"


def login(browser: TestClient, name: str) -> None:
    csrf = browser.get("/admin/auth/csrf").json()["csrf_token"]
    assert (
        browser.post(
            "/admin/auth/login",
            json={"login": name, "password": PASSWORD},
            headers={"Origin": ORIGIN, "X-CSRF-Token": csrf},
        ).status_code
        == 200
    )


def catalog(browser: Any, graph: Linked, status: str) -> None:
    c = graph.c
    detail = browser.get(
        f"/api/v1/catalog/items/{c.item}", params={"model_id": str(c.model)}
    )
    assert detail.status_code == 200
    assert detail.json()["applicability"]["status"] == status
    for offset in (0, 1, 10000):
        result = browser.get(
            "/api/v1/catalog/items",
            params={
                "q": graph.needle,
                "model_id": str(c.model),
                "limit": 1,
                "offset": offset,
            },
        )
        assert result.status_code == 200
        assert result.json()["total"] == (1 if status == "confirmed" else 0)
        assert len(result.json()["items"]) == (
            1 if status == "confirmed" and offset == 0 else 0
        )


@pytest.mark.parametrize("loss", ["version", "document", "internal"])
def test_linked_revoke_propagates_to_all_public_surfaces(
    database: Database,
    pipeline: FilePipeline,
    tmp_path: Path,
    http: Client,
    loss: str,
) -> None:
    graph = linked(database, pipeline, tmp_path)
    c = graph.c
    knowledge = own(http, "knowledge")
    query = request(knowledge, type="text", value=graph.needle)
    quoted = ok(send(http, knowledge, query), "results")
    diagnosis = own(http)
    ok(send(http, diagnosis, start(diagnosis, graph)), "question")
    choice = request(diagnosis, 1, type="button", option_id="a")
    technical = ok(send(http, diagnosis, choice), "results")
    assert technical["reply"]["items"][0]["id"] == str(c.item)
    catalog(http, graph, "confirmed")
    assert http.get(file_url(c.document, c.version)).status_code == 200
    with database.unit_of_work() as session:
        revision = session.get(CatalogItem, c.item).revision  # type: ignore[union-attr]
        report = operation(
            session,
            c.login,
            pipeline,
            "visibility" if loss == "internal" else "revoke",
            c.document,
            c.version if loss == "version" else None,
            visibility="internal" if loss == "internal" else None,
        )
        assert report.ok
    for owner, original, body in (
        (knowledge, quoted, query),
        (diagnosis, technical, choice),
    ):
        replay = ok(send(http, owner, body), "handoff")
        assert replay["message_id"] == original["message_id"]
        read = ok(history(http, owner), "handoff")
        for value in (replay, read):
            no_leak(value, c.section, c.version, c.document, graph.needle, c.item)
            assert value["reply"]["sources"] == value["reply"]["items"] == []
        assert all(m["reply"]["resolution"] == "handoff" for m in read["messages"])
    fresh = ok(
        send(http, knowledge, request(knowledge, 1, type="text", value=graph.needle)),
        "no_match",
    )
    no_leak(fresh, c.section, c.version, c.document)
    ok(
        send(http, diagnosis, request(diagnosis, 2, type="button", option_id="a")),
        "handoff",
    )
    assert http.get(file_url(c.document, c.version)).status_code == 404
    for offset in (0, 1, 10000):
        search = http.get(
            "/api/v1/knowledge/search",
            params={"q": graph.needle, "limit": 1, "offset": offset},
        )
        assert search.status_code == 200 and search.json()["total"] == 0
        no_leak(search.json(), c.section, c.version, c.document, graph.needle)
    docs = http.get("/api/v1/documents", params={"limit": 100})
    assert docs.status_code == 200
    no_leak(docs.json(), c.document, c.version)
    catalog(http, graph, "confirmed" if loss == "internal" else "unknown")
    with database.unit_of_work() as session:
        for identifier in (c.section, uuid4()):
            with pytest.raises(SourceNotFound):
                KnowledgeReadService(session, PUBLIC_VIEWER).get_source(identifier)
        link = session.get(CatalogItemModel, (c.item, c.model))
        assert link is not None
        if loss != "internal":
            assert (
                not link.public_allowed
                and link.published_at is None
                and link.published_by is None
            )
            assert session.get(CatalogItem, c.item).revision == revision + 1  # type: ignore[union-attr]
            assert session.scalar(
                select(AuditEvent.id).where(
                    AuditEvent.entity_id == c.item,
                    AuditEvent.action == "applicability.invalidate",
                )
            )
        # The persisted reply still contains the old logical source; disclosure
        # above must have reprojected it, rather than erasing evidence to pass.
    assert str(c.section) in json.dumps(progress(database, knowledge)[1][0].response)


def test_retired_exact_pin_then_revoke_and_new_publication_never_resurrects_permission(
    database: Database,
    pipeline: FilePipeline,
    tmp_path: Path,
    http: Client,
) -> None:
    graph = linked(database, pipeline, tmp_path)
    c = graph.c
    owner = own(http)
    ok(send(http, owner, start(owner, graph)), "question")
    with database.unit_of_work() as session:
        newer = second_draft(session, c.document, c.version)
        review_publish(
            session, pipeline, tmp_path, c.login, c.document, newer, public=True
        )
    catalog(http, graph, "unknown")
    assert (
        http.get("/api/v1/knowledge/search", params={"q": graph.needle}).json()["total"]
        == 0
    )
    old_file = http.get(file_url(c.document, c.version))
    assert (
        old_file.status_code == 200
        and old_file.headers["x-document-version-status"] == "retired"
    )
    choice = request(owner, 1, type="button", option_id="a")
    result = ok(send(http, owner, choice), "results")
    assert result["reply"]["sources"][0]["document_version_id"] == str(c.version)
    assert "предыдущая редакция" in result["reply"]["sources"][0]["title"]
    assert result["reply"]["items"] == []
    assert ok(history(http, owner))["reply"] == result["reply"]
    new_owner = own(http)
    ok(send(http, new_owner, start(new_owner, graph)), "handoff")
    assert progress(database, owner)[0].tree_id == graph.tree
    with database.unit_of_work() as session:
        # Re-publish a new edition against the same old evidence; the separate
        # fact permission remains cleared even though the document is active.
        link = session.get(CatalogItemModel, (c.item, c.model))
        assert link and not link.public_allowed
        assert operation(session, c.login, pipeline, "revoke", c.document, c.version).ok
    ok(send(http, owner, choice), "handoff")
    ok(history(http, owner), "handoff")
    assert http.get(file_url(c.document, c.version)).status_code == 404
    catalog(http, graph, "unknown")


@pytest.mark.parametrize("kind", ["product", "service"])
def test_internal_evidence_confirms_fact_without_leaks_in_catalog_commerce_and_replay(
    database: Database,
    pipeline: FilePipeline,
    tmp_path: Path,
    http: Client,
    kind: str,
) -> None:
    with database.unit_of_work() as session:
        c, needle = source_corpus(
            session, pipeline, tmp_path, visibility="internal", kind=kind
        )
        confirmed(session, c, tmp_path)
        applicability = service(session).item(c.item, c.model).applicability
        assert applicability is not None and applicability.status == "confirmed"
    graph = Linked(c, needle, uuid4(), "unused", tmp_path, tmp_path)
    catalog(http, graph, "confirmed")
    detail = http.get(
        f"/api/v1/catalog/items/{c.item}", params={"model_id": str(c.model)}
    )
    assert detail.json()["applicability"] == {
        "model_id": str(c.model),
        "status": "confirmed",
        "sources": [],
    }
    owner = own(http, "commerce")
    query = request(owner, type="text", value=needle)
    first = ok(send(http, owner, query), "results")
    assert first["reply"]["items"][0]["id"] == str(c.item)
    for response in (detail, send(http, owner, query), history(http, owner)):
        no_leak(
            response.json(),
            c.document,
            c.version,
            c.section,
            "SYNTHETIC 045 section",
            "PRIVATE045-title-" + needle,
            "PRIVATE045-fragment",
            file_url(c.document, c.version),
        )
    with database.unit_of_work() as session:
        assert operation(session, c.login, pipeline, "revoke", c.document).ok
    catalog(http, graph, "unknown")
    for response in (send(http, owner, query), history(http, owner)):
        assert response.status_code == 200
        no_leak(response.json(), c.document, c.version, c.section)


@pytest.mark.parametrize("loss", ["review", "permission", "evidence", "item", "model"])
def test_internal_fact_requires_every_prerequisite_after_commit(
    database: Database,
    pipeline: FilePipeline,
    tmp_path: Path,
    http: Client,
    loss: str,
) -> None:
    with database.unit_of_work() as session:
        c, needle = source_corpus(session, pipeline, tmp_path, visibility="internal")
        confirmed(session, c, tmp_path)
    with database.unit_of_work() as session:
        if loss in {"review", "permission"}:
            assert command(session, c, "revoke") == "REVOKED"
            if loss == "permission":
                proofs(session, c, tmp_path)  # expert review alone is insufficient
        elif loss == "evidence":
            assert operation(
                session, c.login, pipeline, "retire", c.document, c.version
            ).ok
        else:
            model = CatalogItem if loss == "item" else EquipmentModel
            identifier = c.item if loss == "item" else c.model
            session.execute(
                update(model).where(model.id == identifier).values(status="draft")
            )
    with database.unit_of_work() as session:
        from app.modules.catalog.applicability import public_applicability

        value = session.scalar(
            select(public_applicability(c.model))
            .select_from(CatalogItem)
            .where(CatalogItem.id == c.item)
        )
        assert isinstance(value, dict)
        assert value["status"] == "unknown" and value["sources"] == []
    result = http.get(
        "/api/v1/catalog/items", params={"q": needle, "model_id": str(c.model)}
    )
    assert result.status_code == 200 and result.json()["total"] == 0


def test_acl_status_visibility_allowed_matrix_and_staff_cookie_is_not_chat_authority(
    browsers: tuple[TestClient, TestClient],
    message_db: Database,
    pipeline: FilePipeline,
    tmp_path: Path,
) -> None:
    public, staff = browsers
    needle = "synthetic045" + uuid4().hex
    checked = pipeline.accept(
        io.BytesIO(b"SYNTHETIC ONLY\n"), filename="test.txt", mime="text/plain"
    )
    with message_db.unit_of_work() as session:
        user = admin(session)
        name = user.login
        rows = [
            (
                visibility,
                status,
                allowed,
                edition(
                    session,
                    pipeline,
                    visibility=visibility,
                    status=status,
                    allowed=allowed,
                    reviewer=user.id,
                    title="HIDDEN045-" + uuid4().hex,
                    kind="regulation",
                    content=needle,
                    fixture_file=checked,
                ),
            )
            for visibility in ("public", "internal")
            for status in ("published", "draft", "retired", "revoked")
            for allowed in (False, True)
        ]
        titles = {
            row[0]: row[1]
            for row in session.execute(
                select(Document.id, Document.title).where(
                    Document.id.in_([entry.document for _, _, _, entry in rows])
                )
            )
        }
    app.dependency_overrides[file_pipeline] = lambda: pipeline
    try:
        login(staff, name)
        for browser, count in ((public, 1), (staff, 2)):
            for offset in (0, 1, 2, 10000):
                result = browser.get(
                    "/api/v1/knowledge/search",
                    params={"q": needle, "limit": 1, "offset": offset},
                )
                assert result.status_code == 200 and result.json()["total"] == count
                assert len(result.json()["items"]) == (1 if offset < count else 0)
                for visibility, status, allowed, entry in rows:
                    if (
                        status != "published"
                        or not allowed
                        or (browser is public and visibility == "internal")
                    ):
                        no_leak(
                            result.json(),
                            entry.document,
                            entry.version,
                            entry.section,
                            titles[entry.document],
                            entry.url,
                        )
            owner = own(browser, "knowledge")
            query = request(owner, type="text", value=needle)
            first = ok(send(browser, owner, query), "results")
            assert len(first["reply"]["sources"]) == 1
            for response in (send(browser, owner, query), history(browser, owner)):
                for visibility, status, allowed, entry in rows:
                    if visibility == "internal" or status != "published" or not allowed:
                        no_leak(
                            response.json(),
                            entry.document,
                            entry.version,
                            entry.section,
                            titles[entry.document],
                            entry.url,
                        )
        for visibility, status, allowed, entry in rows:
            for browser in (public, staff):
                available = status in {"published", "retired"} and (
                    visibility == "public" or browser is staff
                )
                response = browser.get(entry.url)
                assert response.status_code == (200 if available else 404)
                if not available:
                    no_leak(
                        response.text,
                        entry.document,
                        entry.version,
                        needle,
                        "SYNTHETIC ONLY",
                        titles[entry.document],
                    )
                viewer = cookie_viewer(browser.cookies.get(SESSION_COOKIE, "") or "")
                with message_db.unit_of_work() as session:
                    reader = KnowledgeReadService(session, viewer)
                    if available and allowed:
                        assert (
                            reader.get_source(entry.section).section_id == entry.section
                        )
                    else:
                        with pytest.raises(SourceNotFound) as denied:
                            reader.get_source(entry.section)
                        with pytest.raises(SourceNotFound) as missing:
                            reader.get_source(uuid4())
                        assert str(denied.value) == str(missing.value)
        # documents total includes approved complete files even when snippets
        # are not assistant_allowed. Compare the two authenticated projections.
        anon_docs = public.get(
            "/api/v1/documents", params={"kind": "regulation", "limit": 100}
        ).json()
        staff_docs = staff.get(
            "/api/v1/documents", params={"kind": "regulation", "limit": 100}
        ).json()
        assert anon_docs["total"] == 2
        assert staff_docs["total"] == 4
        for visibility, status, _, entry in rows:
            if visibility == "internal" or status != "published":
                no_leak(
                    anon_docs, entry.document, entry.version, titles[entry.document]
                )
    finally:
        app.dependency_overrides.clear()


@pytest.mark.parametrize(
    "input",
    [
        {"type": "button", "option_id": "missing"},
        {"type": "button", "option_id": "foreign-node-option"},
        {"type": "button", "option_id": "foreign-tree-option"},
        {"type": "button", "option_id": "foreign-version-option"},
        {"type": "button", "option_id": "a", "next": "result"},
        {"type": "button", "option_id": "a", "node_id": "result"},
        {"type": "button", "option_id": "a", "actor_id": str(uuid4())},
        {"type": "text", "value": "Synthetic A"},
        {"type": "text", "value": "SYNTHETIC result"},
        {"type": "text", "value": "a"},
        {"type": "text", "value": "unrelated SYNTHETIC text"},
    ],
)
def test_real_http_forged_options_and_text_do_not_advance_then_valid_button_does(
    database: Database,
    pipeline: FilePipeline,
    tmp_path: Path,
    http: Client,
    input: dict[str, str],
) -> None:
    foreign = input.get("option_id") == "foreign-node-option"
    graph = linked(database, pipeline, tmp_path, foreign_node=foreign)
    owner = own(http)
    ok(send(http, owner, start(owner, graph)), "question")
    if input.get("option_id") in {"foreign-version-option", "foreign-tree-option"}:
        with database.unit_of_work() as session:
            value = payload(session, graph.c)
            if input["option_id"] == "foreign-version-option":
                value.update(key=graph.key, version=2)
            value["nodes"]["observe"]["options"][0]["id"] = input["option_id"]
            newer, path, proof = import_tree(session, graph.c, tmp_path, value)
            assert run(session, graph.c, newer, "publish", path, proof).ok
    if input.get("value") == "SYNTHETIC result":
        input = {**input, "value": graph.needle + " result"}
    body = request(owner, 1, **input)
    response = send(http, owner, body)
    if set(input) - {"type", "option_id", "value"}:
        assert response.status_code == 422
        version = 1
    else:
        ok(response, "question")
        version = 2
        replay = ok(send(http, owner, body), "question")
        assert replay["message_id"] == response.json()["message_id"]
    row, records = progress(database, owner)
    assert row.current_node_key == "observe" and row.outcome is None
    assert decode_response(records[-1]).state_after.transitions == 0
    ok(
        send(http, owner, request(owner, version, type="button", option_id="a")),
        "question" if foreign else "results",
    )
    row, records = progress(database, owner)
    assert row.tree_id == graph.tree
    assert row.current_node_key == ("other" if foreign else "result")
    assert decode_response(records[-1]).state_after.transitions == 1


def test_real_http_unknown_model_mode_reset_and_fresh_start(
    database: Database,
    pipeline: FilePipeline,
    tmp_path: Path,
    http: Client,
) -> None:
    graph = linked(database, pipeline, tmp_path)
    owner = own(http)
    bad = start(owner, graph)
    bad["input"]["model_id"] = str(uuid4())
    ok(send(http, owner, bad), "handoff")
    assert progress(database, owner)[0].tree_id is None
    ok(send(http, owner, start(owner, graph, 1)), "question")
    ok(
        send(http, owner, request(owner, 2, type="select_mode", mode="commerce")),
        "no_match",
    )
    row, records = progress(database, owner)
    assert row.tree_id is row.current_node_key is row.outcome is None
    assert decode_response(records[-1]).state_after.transitions == 0
    ok(
        send(http, owner, request(owner, 3, type="select_mode", mode="diagnose")),
        "no_match",
    )
    ok(send(http, owner, start(owner, graph, 4)), "question")
    assert progress(database, owner)[0].tree_id == graph.tree


def paths(value: Any) -> list[tuple[list[str], str]]:
    found: list[tuple[list[str], str]] = []
    visited: set[str] = set()

    def walk(key: str, choices: list[str], ancestors: set[str]) -> None:
        assert key not in ancestors, "Cycle in shipped DAG"
        visited.add(key)
        node = value.nodes[key]
        if node.type == "question":
            for option in node.options:
                walk(option.next, [*choices, option.id], ancestors | {key})
        else:
            assert node.type in {"result", "handoff"}
            found.append((choices, key))

    walk(value.start, [], set())
    assert visited == set(value.nodes), "Unreachable shipped node"
    return found


TREE_ROOT = Path("/content/trees")
if not TREE_ROOT.exists():
    TREE_ROOT = Path(__file__).resolve().parents[3] / "content" / "trees"


def test_shipped_corpus_inventory_is_present_and_explicitly_synthetic() -> None:
    assert {p.name for p in (TREE_ROOT / "valid").glob("*.yaml")} == {
        "branching.yaml",
        "handoff.yaml",
        "linear-v1.yaml",
        "linear-v2.yaml",
    }
    assert {p.name for p in (TREE_ROOT / "invalid").glob("*.yaml")} == {
        "action.yaml",
        "alias.yaml",
        "cycle.yaml",
        "duplicate.yaml",
        "tag.yaml",
    }
    assert "SYNTHETIC ONLY" in (TREE_ROOT / "README.md").read_text()


@pytest.mark.parametrize(
    "filename", sorted(p.name for p in (TREE_ROOT / "valid").glob("*.yaml"))
)
@pytest.mark.parametrize("loss", ["none", "source", "recommendation"])
def test_every_shipped_graph_path_through_published_engine_http_and_dependency_loss(
    database: Database,
    pipeline: FilePipeline,
    tmp_path: Path,
    http: Client,
    filename: str,
    loss: str,
) -> None:
    raw = (TREE_ROOT / "valid" / filename).read_bytes()
    assert b"SYNTHETIC ONLY" in raw
    original = parse_tree(raw)
    routes = paths(original)
    assert routes
    with database.unit_of_work() as session:
        c, needle = source_corpus(session, pipeline, tmp_path)
        confirmed(session, c, tmp_path)
        model = session.get(EquipmentModel, c.model)
        assert model is not None
        rebound = (
            raw.decode()
            .replace("11111111-1111-4111-8111-111111111111", str(c.section))
            .replace("22222222-2222-4222-8222-222222222222", str(c.item))
            .replace(original.model_code, model.code)
            .replace(original.key, "SYNTHETIC045-" + uuid4().hex)
        )
        parsed = parse_tree(rebound.encode())
        tree, path, proof = import_tree(
            session, c, tmp_path, parsed.model_dump(exclude_unset=True)
        )
        assert run(session, c, tree, "publish", path, proof).ok
        graph = Linked(c, needle, tree, parsed.key, path, proof)
    completed: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for choices, terminal in routes:
        owner = own(http)
        last = start(owner, graph)
        result = ok(send(http, owner, last))
        for version, option in enumerate(choices, 1):
            assert result["reply"]["resolution"] == "question"
            offered = {b["input"].get("option_id") for b in result["reply"]["buttons"]}
            assert option in offered
            last = request(owner, version, type="button", option_id=option)
            result = ok(send(http, owner, last))
        expected = parsed.nodes[terminal]
        assert result["reply"]["resolution"] == (
            "results" if expected.type == "result" else "handoff"
        )
        if expected.type == "result":
            assert result["reply"]["text"] == expected.text
            assert {s["section_id"] for s in result["reply"]["sources"]} == {
                str(c.section)
            }
            assert [
                (i["id"], i["applicability"]["status"])
                for i in result["reply"]["items"]
            ] == [(str(c.item), "confirmed")]
            form = next(
                a
                for a in result["reply"]["actions"]
                if a["type"] == "open_request_form"
            )
            assert form["payload"]["items"] == [{"item_id": str(c.item), "qty": 1}]
        else:
            assert result["reply"]["sources"] == result["reply"]["items"] == []
            if expected.risk == "expert_only":
                assert result["reply"]["text"] != expected.text
        assert progress(database, owner)[0].current_node_key == terminal
        completed.append((owner, last))
    if loss != "none":
        with database.unit_of_work() as session:
            if loss == "source":
                assert operation(session, c.login, pipeline, "revoke", c.document).ok
            else:
                assert command(session, c, "revoke") == "REVOKED"
    for owner, last in completed:
        result = ok(send(http, owner, last))
        restored = ok(history(http, owner))
        if loss == "source":
            assert result["reply"]["resolution"] == "handoff"
            no_leak(restored, c.section, c.version, c.document, c.item)
        if loss != "none":
            assert result["reply"]["items"] == []
        assert restored["reply"] == result["reply"]


@pytest.mark.parametrize(
    "filename", sorted(p.name for p in (TREE_ROOT / "invalid").glob("*.yaml"))
)
def test_every_shipped_invalid_yaml_is_rejected(filename: str) -> None:
    with pytest.raises(TreeRejected):
        parse_tree((TREE_ROOT / "invalid" / filename).read_bytes())


@pytest.mark.parametrize("publication", ["applicability", "tree"])
def test_revoke_overlaps_dependency_publication_and_cannot_resurrect(
    database: Database,
    pipeline: FilePipeline,
    tmp_path: Path,
    http: Client,
    publication: str,
) -> None:
    graph = linked(database, pipeline, tmp_path, publish=False)
    c = graph.c
    permission = None
    with database.unit_of_work() as session:
        if publication == "applicability":
            assert command(session, c, "revoke") == "REVOKED"
            _, permission = proofs(session, c, tmp_path)
        else:
            applicability = service(session).item(c.item, c.model).applicability
            assert applicability and applicability.status == "confirmed"

    def publish() -> str:
        with database.unit_of_work() as session:
            if publication == "applicability":
                assert permission is not None
                return command(session, c, "publish", proof=permission)
            return run(session, c, graph.tree, "publish", graph.path, graph.proof).code

    with ThreadPoolExecutor(max_workers=1) as pool:
        with database.unit_of_work() as holder:
            holder.scalar(
                select(Document.id).where(Document.id == c.document).with_for_update()
            )
            pid = holder.scalar(text("SELECT pg_backend_pid()"))
            assert pid is not None
            pending = pool.submit(publish)
            wait_for_blocker(database, pid)
            actor = holder.get(StaffUser, c.actor)
            assert actor is not None
            # Exercise document -> dependency lock order underneath the shared
            # CLI staff lock. Identity is the real fixture's verified admin;
            # the service still refreshes role and uses its actual invalidator.
            assert (
                LifecycleService(holder, AuthenticatedStaff(actor), pipeline)
                .execute("revoke", c.document)
                .ok
            )
        assert pending.result(timeout=5) in {
            "EVIDENCE_UNAVAILABLE",
            "PUBLICATION_BLOCKED",
        }
    with database.unit_of_work() as session:
        link = session.get(CatalogItemModel, (c.item, c.model))
        assert link is not None and not link.public_allowed
        assert session.get(DiagnosticTree, graph.tree).status == "draft"  # type: ignore[union-attr]
        assert load_tree(session, tree_id=graph.tree) is None
    catalog(http, graph, "unknown")


def test_visibility_change_commits_during_real_file_scan_and_no_bytes_escape(
    database: Database,
    pipeline: FilePipeline,
    tmp_path: Path,
    http: Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph = linked(database, pipeline, tmp_path)
    entered, release = Event(), Event()
    scan = pipeline.scanner.scan

    def held(*args: Any, **kwargs: Any) -> Any:
        result = scan(*args, **kwargs)  # actual ClamAV succeeds before pause
        entered.set()
        assert release.wait(5)
        return result

    monkeypatch.setattr(pipeline.scanner, "scan", held)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(http.get, file_url(graph.c.document, graph.c.version))
        try:
            assert entered.wait(5)
            with database.unit_of_work() as session:
                assert operation(
                    session,
                    graph.c.login,
                    pipeline,
                    "visibility",
                    graph.c.document,
                    visibility="internal",
                ).ok
        finally:
            release.set()
        response = pending.result(timeout=5)
    assert response.status_code == 404
    no_leak(response.text, "Synthetic text", graph.c.document, graph.c.version)
    catalog(http, graph, "confirmed")


@pytest.mark.parametrize("read_kind", ["history", "replay"])
def test_revoke_during_persisted_projection_has_coherent_snapshot_and_next_read_hides(
    database: Database,
    pipeline: FilePipeline,
    tmp_path: Path,
    http: Client,
    read_kind: str,
) -> None:
    graph = linked(database, pipeline, tmp_path)
    owner = own(http)
    body = start(owner, graph)
    ok(send(http, owner, body), "question")
    entered, release = Event(), Event()

    def pause(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        if "FROM app.chat_sessions" in statement and "FOR SHARE" in statement:
            entered.set()
            assert release.wait(5)

    event.listen(database.engine, "after_cursor_execute", pause)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = (
                pool.submit(history, http, owner)
                if read_kind == "history"
                else pool.submit(send, http, owner, body)
            )
            try:
                assert entered.wait(5)
                with database.unit_of_work() as session:
                    assert operation(
                        session, graph.c.login, pipeline, "revoke", graph.c.document
                    ).ok
            finally:
                release.set()
            first = ok(pending.result(timeout=5), "question")
            assert first["reply"]["sources"]
    finally:
        event.remove(database.engine, "after_cursor_execute", pause)
    for response in (send(http, owner, body), history(http, owner)):
        result = ok(response, "handoff")
        no_leak(result, graph.c.section, graph.c.document, graph.needle)


@pytest.mark.parametrize("loss", ["logout", "disable", "expiry"])
def test_staff_authority_loss_closes_internal_reads_but_public_chat_remains_public(
    browsers: tuple[TestClient, TestClient],
    message_db: Database,
    pipeline: FilePipeline,
    tmp_path: Path,
    loss: str,
) -> None:
    public, staff = browsers
    with message_db.unit_of_work() as session:
        c, needle = source_corpus(session, pipeline, tmp_path, visibility="internal")
    login(staff, c.login)
    cookie = staff.cookies.get(SESSION_COOKIE)
    assert (
        staff.get("/api/v1/knowledge/search", params={"q": needle}).json()["total"] == 1
    )
    if loss == "logout":
        csrf = staff.get("/admin/auth/csrf").json()["csrf_token"]
        logged_out = staff.post(
            "/admin/auth/logout", headers={"Origin": ORIGIN, "X-CSRF-Token": csrf}
        )
        assert logged_out.status_code == 204
        assert cookie is not None
        staff.cookies.set(SESSION_COOKIE, cookie)
    else:
        with message_db.unit_of_work() as session:
            if loss == "disable":
                assert (
                    disable_staff(
                        session,
                        admin_login=c.login,
                        admin_password=PASSWORD,
                        user_id=c.actor,
                    ).result
                    == "success"
                )
            else:
                session.execute(
                    update(StaffSession)
                    .where(StaffSession.user_id == c.actor)
                    .values(expires_at=utc_now() - timedelta(seconds=1))
                )
    app.dependency_overrides[file_pipeline] = lambda: pipeline
    try:
        assert staff.get(file_url(c.document, c.version)).status_code == 404
        assert (
            staff.get("/api/v1/knowledge/search", params={"q": needle}).json()["total"]
            == 0
        )
        for browser in (public, staff):
            owner = own(browser, "knowledge")
            body = request(owner, type="text", value=needle)
            ok(send(browser, owner, body), "no_match")
            for response in (send(browser, owner, body), history(browser, owner)):
                no_leak(response.json(), c.section, c.document, c.version, needle)
    finally:
        app.dependency_overrides.clear()


@pytest.mark.parametrize("identical", [False, True])
def test_tcp_parallel_messages_commit_one_transition_and_body_conflict(
    database: Database,
    pipeline: FilePipeline,
    tmp_path: Path,
    http: Client,
    monkeypatch: pytest.MonkeyPatch,
    identical: bool,
) -> None:
    graph = linked(database, pipeline, tmp_path)
    owner = own(http)
    ok(send(http, owner, start(owner, graph)), "question")
    first = request(owner, 1, type="button", option_id="a")
    second = (
        deepcopy(first)
        if identical
        else request(owner, 1, type="button", option_id="b")
    )
    entered, release = Event(), Event()
    route = ModeRouter.route
    pids: list[int] = []

    def pause(self: ModeRouter, *args: Any, **kwargs: Any) -> Any:
        pid = self.session.scalar(text("SELECT pg_backend_pid()"))
        assert pid is not None
        pids.append(pid)
        entered.set()
        assert release.wait(5)
        return route(self, *args, **kwargs)

    monkeypatch.setattr(ModeRouter, "route", pause)
    with ThreadPoolExecutor(max_workers=2) as pool:
        winner = pool.submit(send, http, owner, first)
        try:
            assert entered.wait(5)
            loser = pool.submit(send, http, owner, second)
            wait_for_blocker(database, pids[0])
        finally:
            release.set()
        result = ok(winner.result(timeout=5), "results")
        other = loser.result(timeout=5)
    if identical:
        assert ok(other) == result
    else:
        assert (
            other.status_code == 409
            and other.json()["error"]["code"] == "STATE_CONFLICT"
        )
    row, records = progress(database, owner)
    assert len(pids) == 1 and len(records) == row.state_version == 2
    assert row.current_node_key == "result" and row.tree_id == graph.tree
    assert decode_response(records[-1]).state_after.transitions == 1
    changed = deepcopy(first)
    changed["input"]["option_id"] = "b"
    conflict = send(http, owner, changed)
    assert (
        conflict.status_code == 409
        and conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    )
    assert ok(history(http, owner))["reply"] == result["reply"]


def test_new_tree_publication_overlaps_running_http_session_without_replacing_pin(
    database: Database,
    pipeline: FilePipeline,
    tmp_path: Path,
    http: Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph = linked(database, pipeline, tmp_path)
    c = graph.c
    owner = own(http)
    ok(send(http, owner, start(owner, graph)), "question")
    with database.unit_of_work() as session:
        value = payload(session, c)
        value.update(key=graph.key, version=2)
        newer, path, proof = import_tree(session, c, tmp_path, value)
    entered, release = Event(), Event()
    route = ModeRouter.route

    def held(self: ModeRouter, *args: Any, **kwargs: Any) -> Any:
        result = route(self, *args, **kwargs)
        entered.set()
        assert release.wait(5)
        return result

    monkeypatch.setattr(ModeRouter, "route", held)
    choice = request(owner, 1, type="button", option_id="a")
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(send, http, owner, choice)
        try:
            assert entered.wait(5)
            with database.unit_of_work() as session:
                assert run(session, c, newer, "publish", path, proof).ok
        finally:
            release.set()
        result = ok(pending.result(timeout=5), "results")
    assert progress(database, owner)[0].tree_id == graph.tree
    assert result["reply"]["text"] == graph.needle + " result"
    assert ok(send(http, owner, choice))["reply"] == result["reply"]
    fresh = own(http)
    ok(send(http, fresh, start(fresh, graph)), "question")
    assert progress(database, fresh)[0].tree_id == newer
    with database.unit_of_work() as session:
        assert session.get(DiagnosticTree, graph.tree).status == "retired"  # type: ignore[union-attr]


@pytest.mark.parametrize(
    "boundary", ["document", "applicability", "tree", "audit", "commit"]
)
def test_linked_revoke_fault_rolls_back_status_permission_revision_audit_and_projection(
    database: Database,
    pipeline: FilePipeline,
    tmp_path: Path,
    http: Client,
    boundary: str,
) -> None:
    graph = linked(database, pipeline, tmp_path)
    c = graph.c
    owner = own(http)
    body = start(owner, graph)
    before_reply = ok(send(http, owner, body), "question")

    def snapshot() -> tuple[Any, ...]:
        with database.unit_of_work() as session:
            link = session.get(CatalogItemModel, (c.item, c.model))
            item = session.get(CatalogItem, c.item)
            doc = session.get(Document, c.document)
            tree = session.get(DiagnosticTree, graph.tree)
            assert link and item and doc and tree
            events = list(
                session.execute(
                    select(AuditEvent.id, AuditEvent.result)
                    .where(
                        AuditEvent.entity_id.in_(
                            [c.item, c.document, c.version, graph.tree]
                        )
                    )
                    .order_by(AuditEvent.id)
                )
            )
            return (
                doc.status,
                link.public_allowed,
                link.published_by,
                link.published_at,
                item.revision,
                tree.status,
                events,
            )

    before = snapshot()
    # Revocation never writes immutable trees. The tree boundary therefore
    # injects a failed replacement publication in this same dependency graph.
    if boundary == "tree":
        with database.unit_of_work() as session:
            value = payload(session, c)
            value.update(key=graph.key, version=2)
            newer, path, proof = import_tree(session, c, tmp_path, value)
        table, verb = "diag_trees", "UPDATE"
    else:
        table, verb = {
            "document": ("documents", "UPDATE"),
            "applicability": ("catalog_item_models", "UPDATE"),
            "audit": ("audit_events", "INSERT INTO"),
            "commit": ("documents", "UPDATE"),
        }[boundary]
    with pg_fault(database, table, verb=verb, commit=boundary == "commit") as reached:
        with pytest.raises(SQLAlchemyError), database.unit_of_work() as session:
            if boundary == "tree":
                run(session, c, newer, "publish", path, proof)
            else:
                operation(session, c.login, pipeline, "revoke", c.document)
    assert reached[0]
    assert snapshot() == before
    assert ok(send(http, owner, body))["reply"] == before_reply["reply"]
    catalog(http, graph, "confirmed")
    with database.unit_of_work() as session:
        assert operation(session, c.login, pipeline, "revoke", c.document).ok
    ok(history(http, owner), "handoff")
    catalog(http, graph, "unknown")


@pytest.mark.parametrize(
    "role,active",
    [("manager", True), ("manager", False), ("admin", False), ("admin", True)],
)
def test_real_sqladmin_direct_forms_and_cli_share_authority_without_actor_spoof(
    browsers: tuple[TestClient, TestClient],
    message_db: Database,
    pipeline: FilePipeline,
    tmp_path: Path,
    role: str,
    active: bool,
) -> None:
    anonymous, staff = browsers
    graph = linked(message_db, pipeline, tmp_path)
    c = graph.c
    with message_db.unit_of_work() as session:
        user = admin(session, role=role)
        login_name, user_id = user.login, user.id
    login(staff, login_name)
    csrf = staff.get("/admin/auth/csrf").json()["csrf_token"]
    if not active:
        with message_db.unit_of_work() as session:
            assert (
                disable_staff(
                    session,
                    admin_login=c.login,
                    admin_password=PASSWORD,
                    user_id=user_id,
                ).result
                == "success"
            )
    url = f"/admin/catalog/items/{c.item}/applicability"
    with message_db.unit_of_work() as session:
        revision = session.get(CatalogItem, c.item).revision  # type: ignore[union-attr]
    form = {
        "model_id": str(c.model),
        "expected_revision": str(revision),
        "csrf_token": csrf,
    }
    for action in ("candidate", "replace", "review", "publish", "revoke"):
        assert (
            anonymous.post(
                url + "/" + action,
                data=form,
                headers={"Origin": ORIGIN},
                follow_redirects=False,
            ).status_code
            == 401
        )
        denied = staff.post(
            url + "/" + action,
            data={**form, "actor_id": str(c.actor)},
            headers={"Origin": ORIGIN},
            follow_redirects=False,
        )
        assert denied.status_code == (
            422 if active and role == "admin" else 403 if active else 401
        )
    assert staff.get("/admin/orders", follow_redirects=False).status_code == (
        200 if active else 401
    )
    arguments = ["tree-revoke", "--tree", str(graph.tree)]
    code, output = cli_request(
        pipeline, login_name, arguments + ["--actor-id", str(c.actor)]
    )
    assert code != 0 and b'"code": "REVOKED"' not in output
    code, output = cli_request(pipeline, login_name, arguments)
    assert code == (0 if active and role == "admin" else 1)
    assert PASSWORD.encode() not in output
    with message_db.unit_of_work() as session:
        tree = session.get(DiagnosticTree, graph.tree)
        assert tree is not None
        assert tree.status == ("revoked" if active and role == "admin" else "published")
        assert session.get(CatalogItem, c.item).revision == revision  # type: ignore[union-attr]
    if active and role == "admin":
        assert (
            staff.post(
                url + "/revoke",
                data=form,
                headers={"Origin": "https://untrusted.invalid"},
                follow_redirects=False,
            ).status_code
            == 403
        )
        assert (
            staff.post(
                url + "/revoke",
                data={k: v for k, v in form.items() if k != "csrf_token"},
                headers={"Origin": ORIGIN},
                follow_redirects=False,
            ).status_code
            == 403
        )
        assert (
            staff.post(
                url + "/revoke",
                data=form,
                headers={"Origin": ORIGIN},
                follow_redirects=False,
            ).status_code
            == 303
        )
        with message_db.unit_of_work() as session:
            assert session.get(CatalogItem, c.item).revision == revision + 1  # type: ignore[union-attr]
            audit = session.scalar(
                select(AuditEvent).where(
                    AuditEvent.entity_id == c.item,
                    AuditEvent.action == "applicability.revoke",
                )
            )
            assert audit and audit.actor_id == user_id


def test_staff_disable_overlaps_actual_cli_before_authentication_completes(
    database: Database,
    pipeline: FilePipeline,
    tmp_path: Path,
) -> None:
    graph = linked(database, pipeline, tmp_path)
    with database.unit_of_work() as session:
        target = admin(session)
        target_id, name = target.id, target.login
    with ThreadPoolExecutor(max_workers=1) as pool:
        with database.unit_of_work() as holder:
            assert (
                disable_staff(
                    holder,
                    admin_login=graph.c.login,
                    admin_password=PASSWORD,
                    user_id=target_id,
                ).result
                == "success"
            )
            pid = holder.scalar(text("SELECT pg_backend_pid()"))
            assert pid is not None
            pending = pool.submit(
                cli_request, pipeline, name, ["tree-revoke", "--tree", str(graph.tree)]
            )
            wait_for_blocker(database, pid)
        code, output = pending.result(timeout=10)
    assert code == 1 and b"AUTHENTICATION_DENIED" in output
    with database.unit_of_work() as session:
        assert session.get(DiagnosticTree, graph.tree).status == "published"  # type: ignore[union-attr]
        assert not session.scalar(
            select(AuditEvent.id).where(
                AuditEvent.entity_id == graph.tree,
                AuditEvent.action == "tree.revoke",
                AuditEvent.result == "success",
            )
        )


def test_evidence_replacement_overlaps_repermission_without_restoring_old_approval(
    database: Database,
    pipeline: FilePipeline,
    tmp_path: Path,
    http: Client,
) -> None:
    graph = linked(database, pipeline, tmp_path)
    c = graph.c
    with database.unit_of_work() as session:
        alternative, _ = source_corpus(
            session, pipeline, tmp_path, visibility="internal"
        )
        assert command(session, c, "revoke") == "REVOKED"
        _, proof = proofs(session, c, tmp_path)

    def publish() -> str:
        with database.unit_of_work() as session:
            return command(session, c, "publish", proof=proof)

    with ThreadPoolExecutor(max_workers=1) as pool:
        with database.unit_of_work() as holder:
            # The service's sorted parent locks cover both old and replacement
            # evidence; the contender has already probed the old binding.
            holder.execute(
                select(Document.id)
                .where(Document.id.in_([c.document, alternative.document]))
                .order_by(Document.id)
                .with_for_update()
            )
            pid = holder.scalar(text("SELECT pg_backend_pid()"))
            assert pid is not None
            pending = pool.submit(publish)
            wait_for_blocker(database, pid)
            user = holder.get(StaffUser, c.actor)
            assert user
            result = ApplicabilityWriteService(
                holder, AuthenticatedStaff(user)
            ).execute("replace", c.item, c.model, section_id=alternative.section)
            assert result.ok
        assert pending.result(timeout=5) == "CANDIDATE_CHANGED"
    catalog(http, graph, "unknown")
    with database.unit_of_work() as session:
        link = session.get(CatalogItemModel, (c.item, c.model))
        assert link and link.evidence_section_id == alternative.section
        assert not link.public_allowed and link.approved_at is None
        assert link.published_by is None and link.published_at is None


def test_thirty_edges_survive_actual_api_process_restart_and_replay(
    database: Database,
    pipeline: FilePipeline,
    tmp_path: Path,
    http: Client,
) -> None:
    with database.unit_of_work() as session:
        c, needle = source_corpus(session, pipeline, tmp_path)
        confirmed(session, c, tmp_path)
        value = payload(session, c)
        value["start"] = "q0"
        value["nodes"] = {
            "result": value["nodes"]["result"],
            "handoff": value["nodes"]["handoff"],
        }
        for index in range(30):
            value["nodes"][f"q{index}"] = {
                "type": "question",
                "risk": "observation",
                "text": "SYNTHETIC ONLY observation",
                "source_refs": [str(c.section)],
                "options": [
                    {
                        "id": "a",
                        "text": "SYNTHETIC continue",
                        "next": f"q{index + 1}" if index < 29 else "result",
                    },
                    {"id": "b", "text": "SYNTHETIC handoff", "next": "handoff"},
                ],
            }
        tree, path, proof = import_tree(session, c, tmp_path, value)
        assert run(session, c, tree, "publish", path, proof).ok
        graph = Linked(c, needle, tree, value["key"], path, proof)
    owner = own(http)
    ok(send(http, owner, start(owner, graph)), "question")
    last: dict[str, Any] = {}
    result: dict[str, Any] = {}

    def advance(browser: Client, first: int, end: int) -> None:
        nonlocal last, result
        for count in range(first, end):
            version = progress(database, owner)[0].state_version
            if count == 30:
                # Simulate the next quota window for this synthetic session only;
                # neither the quota limit nor the engine edge limit is changed.
                with database.unit_of_work() as session:
                    session.execute(
                        text(
                            "UPDATE app.rate_limit_buckets SET "
                            "started_at=started_at-interval '2 minutes', "
                            "expires_at=expires_at-interval '2 minutes' "
                            "WHERE scope='message' AND subject_hash=:subject"
                        ),
                        {
                            "subject": hashlib.sha256(
                                UUID(owner["session_id"]).bytes
                            ).hexdigest()
                        },
                    )
            last = request(owner, version, type="button", option_id="a")
            result = ok(
                send(browser, owner, last), "results" if count == 30 else "question"
            )
            row, records = progress(database, owner)
            assert row.tree_id == tree
            assert row.current_node_key == ("result" if count == 30 else f"q{count}")
            assert decode_response(records[-1]).state_after.transitions == count

    advance(http, 1, 16)
    # A fresh OS process imports the app and opens its own DB pool. All private
    # counters must come from committed envelopes, not parent Python objects.
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        child = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app.main:app",
                "--fd",
                str(listener.fileno()),
                "--no-access-log",
                "--no-proxy-headers",
                "--log-level",
                "error",
            ],
            pass_fds=(listener.fileno(),),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            with Client(
                base_url=f"http://127.0.0.1:{listener.getsockname()[1]}",
                trust_env=False,
                timeout=5,
            ) as restarted:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    try:
                        if restarted.get("/readyz", timeout=0.2).status_code == 200:
                            break
                    except RequestError:
                        pass
                    assert child.poll() is None
                else:
                    pytest.fail("Restarted API did not become ready")
                assert ok(send(restarted, owner, last)) == result
                advance(restarted, 16, 31)
                assert ok(send(restarted, owner, last)) == result
                version = progress(database, owner)[0].state_version
                ok(
                    send(
                        restarted,
                        owner,
                        request(owner, version, type="button", option_id="a"),
                    ),
                    "results",
                )
                assert (
                    decode_response(
                        progress(database, owner)[1][-1]
                    ).state_after.transitions
                    == 30
                )
        finally:
            child.terminate()
            child.wait(timeout=5)
    assert ok(history(http, owner))["reply"] == result["reply"]
