"""MVP-027: actual PostgreSQL 16 constraints, locks, UoW and hidden-input CLI."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import event, func, select, text
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.orm import Session
from test_catalog_applicability import Corpus, corpus, wait_for_lock
from test_kb_import import PASSWORD, admin
from test_kb_import import pipeline as pipeline
from test_kb_import import session as session
from test_kb_lifecycle import cli_request

from app.core.db import Database
from app.core.staff import disable_staff
from app.core.staff_models import AuditEvent
from app.modules.catalog.models import CatalogItemModel, EquipmentModel
from app.modules.kb.files import FilePipeline
from app.modules.kb.models import Document, DocumentSection
from app.modules.kb.tree_models import (
    DiagnosticTree,
    DiagnosticTreeItem,
    DiagnosticTreeSource,
)
from app.modules.kb.tree_service import TreeReport, run_tree

pytestmark = pytest.mark.integration
COMMIT = "a" * 40


@pytest.mark.parametrize(
    "case",
    [
        "duplicate_yaml",
        "tag",
        "alias",
        "depth",
        "size",
        "unknown",
        "no_start",
        "bad_start",
        "unreachable",
        "cycle",
        "target",
        "one_option",
        "option_id",
        "node_type",
        "risk",
        "expert_question",
        "expert_result",
        "no_source",
        "duplicate_source",
        "duplicate_item",
        "qty",
        "hidden_action",
        "unicode",
    ],
)
def test_adversarial_import_corpus(session: Session, tmp_path: Path, case: str) -> None:
    c = corpus(session)
    value = payload(session, c)
    question = value["nodes"]["observe"]
    if case == "unknown":
        value["actor_id"] = str(c.actor)
    elif case == "no_start":
        del value["start"]
    elif case == "bad_start":
        value["start"] = "missing"
    elif case == "unreachable":
        value["nodes"]["extra"] = value["nodes"]["handoff"]
    elif case in {"cycle", "target"}:
        question["options"][0]["next"] = "observe" if case == "cycle" else "missing"
    elif case == "one_option":
        question["options"].pop()
    elif case == "option_id":
        question["options"][1]["id"] = "a"
    elif case == "node_type":
        question["type"] = "execute"
    elif case == "risk":
        question["risk"] = "safe"
    elif case == "expert_question":
        question["risk"] = "expert_only"
    elif case == "expert_result":
        value["nodes"]["result"]["risk"] = "expert_only"
    elif case == "no_source":
        question["source_refs"] = []
    elif case == "duplicate_source":
        question["source_refs"] *= 2
    elif case == "duplicate_item":
        value["nodes"]["result"]["recommendations"] *= 2
    elif case == "qty":
        value["nodes"]["result"]["recommendations"][0]["qty"] = -1
    elif case == "hidden_action":
        question["action"] = "https://example.invalid"
    elif case == "unicode":
        question["text"] = "SYNTHETIC\u202e"
    data = json.dumps(value).encode()
    data = {
        "duplicate_yaml": b"key: SYNTHETIC\nkey: DUPLICATE\n",
        "tag": b"!!python/object/apply:os.system ['echo synthetic']",
        "alias": b"a: &a [1]\nb: &b [*a,*a]\nc: [*b,*b]",
        "depth": b"x: " + b"[" * 20 + b"0" + b"]" * 20,
        "size": b"x" * 262145,
    }.get(case, data)
    path = tmp_path / "synthetic.yaml"
    path.write_bytes(data)
    report = invoke(session, c, path)
    assert not report.ok
    assert not session.scalar(select(DiagnosticTree.id))
    audit = session.scalar(select(AuditEvent).where(AuditEvent.action == "tree.import"))
    assert audit and audit.result == report.code
    assert "echo synthetic" not in json.dumps(report.technical()) + json.dumps(
        audit.details
    )


def payload(session: Session, c: Corpus) -> dict[str, Any]:
    code = session.scalar(
        select(EquipmentModel.code).where(EquipmentModel.id == c.model)
    )
    return {
        "key": "SYNTHETIC027-" + uuid4().hex,
        "version": 1,
        "model_code": code,
        "title": "SYNTHETIC ONLY",
        "start": "observe",
        "nodes": {
            "observe": {
                "type": "question",
                "risk": "observation",
                "text": "SYNTHETIC choice",
                "source_refs": [str(c.section)],
                "options": [
                    {"id": "a", "text": "Synthetic A", "next": "result"},
                    {"id": "b", "text": "Synthetic B", "next": "handoff"},
                ],
            },
            "result": {
                "type": "result",
                "risk": "observation",
                "text": "SYNTHETIC result",
                "source_refs": [str(c.section)],
                "recommendations": [{"item_id": str(c.item), "qty": 2}],
            },
            "handoff": {
                "type": "handoff",
                "risk": "expert_only",
                "text": "SYNTHETIC ONLY: contact a specialist.",
            },
        },
    }


def write(path: Path, value: dict[str, Any]) -> Path:
    path.write_text(json.dumps(value))
    return path


def invoke(session: Session, c: Corpus, path: Path) -> TreeReport:
    return run_tree(
        session,
        login=c.login,
        password=PASSWORD,
        operation="import",
        path=path,
        git_commit=COMMIT,
    )


def test_import_check_normalization_audit_and_no_side_effects(
    session: Session, tmp_path: Path
) -> None:
    c = corpus(session)
    before = {
        table: session.execute(text(f"SELECT * FROM app.{table} ORDER BY 1")).all()
        for table in (
            "documents",
            "document_versions",
            "document_sections",
            "catalog_items",
            "equipment_models",
            "catalog_item_models",
            "orders",
            "order_items",
            "email_outbox",
            "consents",
        )
    }
    path = write(tmp_path / "synthetic.yaml", payload(session, c))
    report = invoke(session, c, path)
    assert report.code == "IMPORTED_DRAFT" and report.tree_id
    saved = session.get(DiagnosticTree, report.tree_id)
    assert saved and saved.status == "draft"
    assert not any(
        (
            saved.approved_at,
            saved.published_at,
            saved.published_by,
            saved.expert_name,
            saved.review_reference,
        )
    )
    assert saved.git_commit == COMMIT
    assert "source_refs" not in json.dumps(saved.graph)
    assert "recommendations" not in json.dumps(saved.graph)
    assert (
        len(
            list(
                session.scalars(
                    select(DiagnosticTreeSource).where(
                        DiagnosticTreeSource.tree_id == saved.id
                    )
                )
            )
        )
        == 2
    )
    item = session.scalar(
        select(DiagnosticTreeItem).where(DiagnosticTreeItem.tree_id == saved.id)
    )
    assert item and item.item_id == c.item and item.qty == 2
    assert {b.code for b in report.blockers} == {
        "EXPERT_REVIEW_REQUIRED",
        "APPLICABILITY_NOT_CONFIRMED",
    }
    check = run_tree(
        session, login=c.login, password=PASSWORD, operation="check", tree_id=saved.id
    )
    assert check.code == "STRUCTURALLY_VALID_DRAFT"
    assert invoke(session, c, path).code == "NO_OP"
    audit = session.scalar(
        select(AuditEvent).where(
            AuditEvent.entity_id == saved.id, AuditEvent.result == "IMPORTED_DRAFT"
        )
    )
    assert audit and audit.actor_id == c.actor and audit.details["binding_sha256"]
    assert "INTERNAL-SYNTHETIC" not in json.dumps(report.technical())
    assert "SYNTHETIC choice" not in json.dumps(audit.details)
    for table, rows in before.items():
        assert (
            session.execute(text(f"SELECT * FROM app.{table} ORDER BY 1")).all() == rows
        )


@pytest.mark.parametrize("kind", ["product", "service"])
def test_confirmed_predicate_and_current_source_acl(
    session: Session, tmp_path: Path, kind: str
) -> None:
    c = corpus(session, kind=kind)
    now = datetime.now(UTC)
    session.add(
        CatalogItemModel(
            item_id=c.item,
            model_id=c.model,
            evidence_section_id=c.section,
            expert_name="SYNTHETIC ONLY",
            review_reference="SYNTHETIC:027",
            approved_at=now,
            public_allowed=True,
            published_by=c.actor,
            published_at=now,
        )
    )
    session.flush()
    path = write(tmp_path / "synthetic.yaml", payload(session, c))
    report = invoke(session, c, path)
    assert report.code == "IMPORTED_DRAFT"
    assert {b.code for b in report.blockers} == {"EXPERT_REVIEW_REQUIRED"}
    session.execute(
        text("UPDATE app.documents SET visibility='internal' WHERE id=:id"),
        {"id": c.document},
    )
    check = run_tree(
        session,
        login=c.login,
        password=PASSWORD,
        operation="check",
        tree_id=report.tree_id,
    )
    assert {b.code for b in check.blockers} == {
        "EXPERT_REVIEW_REQUIRED",
        "SOURCE_NOT_PUBLIC",
    }
    session.execute(
        text("UPDATE app.document_versions SET status='revoked' WHERE id=:id"),
        {"id": c.version},
    )
    check = run_tree(
        session,
        login=c.login,
        password=PASSWORD,
        operation="check",
        tree_id=report.tree_id,
    )
    assert {b.code for b in check.blockers} == {
        "EXPERT_REVIEW_REQUIRED",
        "SOURCE_NOT_PUBLIC",
        "APPLICABILITY_NOT_CONFIRMED",
    }
    assert session.get(DiagnosticTree, report.tree_id).status == "draft"  # type: ignore[union-attr]


@pytest.mark.parametrize(
    "state", ["internal", "revoked_document", "retired", "revoked", "not_allowed"]
)
def test_unavailable_sources_are_explicit_blockers(
    session: Session, tmp_path: Path, state: str
) -> None:
    c = corpus(
        session,
        visibility="internal" if state == "internal" else "public",
        allowed=state != "not_allowed",
    )
    if state == "revoked_document":
        session.execute(
            text("UPDATE app.documents SET status='revoked' WHERE id=:id"),
            {"id": c.document},
        )
    if state in {"retired", "revoked"}:
        session.execute(
            text("UPDATE app.document_versions SET status=:state WHERE id=:id"),
            {"id": c.version, "state": state},
        )
    report = invoke(session, c, write(tmp_path / "tree.yaml", payload(session, c)))
    assert report.code == "IMPORTED_DRAFT"
    assert "SOURCE_NOT_PUBLIC" in {b.code for b in report.blockers}
    assert "INTERNAL-SYNTHETIC-TEXT" not in json.dumps(report.technical())


@pytest.mark.parametrize(
    "case", ["section", "item", "model", "commit", "invalid_graph"]
)
def test_structural_refusals_are_atomic(
    session: Session, tmp_path: Path, case: str
) -> None:
    c = corpus(session)
    value = payload(session, c)
    if case == "section":
        value["nodes"]["result"]["source_refs"] = [str(uuid4())]
    elif case == "item":
        value["nodes"]["result"]["recommendations"][0]["item_id"] = str(uuid4())
    elif case == "model":
        value["model_code"] = "SYNTHETIC-MISSING"
    elif case == "invalid_graph":
        value["nodes"]["observe"]["options"][0]["next"] = "observe"
    path = write(tmp_path / "tree.yaml", value)
    report = run_tree(
        session,
        login=c.login,
        password=PASSWORD,
        operation="import",
        path=path,
        git_commit="main" if case == "commit" else COMMIT,
    )
    assert not report.ok
    assert (
        report.code
        == {
            "section": "SOURCE_NOT_FOUND",
            "item": "ITEM_NOT_FOUND",
            "model": "MODEL_NOT_FOUND",
            "commit": "COMMIT_INVALID",
            "invalid_graph": "GRAPH_INVALID",
        }[case]
    )
    assert not session.scalar(
        select(DiagnosticTree.id).where(DiagnosticTree.key == value["key"])
    )
    assert session.scalar(select(AuditEvent.id).where(AuditEvent.result == report.code))


def test_versions_checksum_and_model_conflicts(
    session: Session, tmp_path: Path
) -> None:
    c = corpus(session)
    value = payload(session, c)
    path = write(tmp_path / "tree.yaml", value)
    first = invoke(session, c, path)
    assert first.ok
    path.write_text(path.read_text() + "\n")
    assert invoke(session, c, path).code == "CHECKSUM_CONFLICT"
    value["version"] = 2
    assert invoke(session, c, write(path, value)).code == "IMPORTED_DRAFT"
    other = corpus(session)
    value["version"] = 3
    value["model_code"] = session.scalar(
        select(EquipmentModel.code).where(EquipmentModel.id == other.model)
    )
    assert invoke(session, c, write(path, value)).code == "MODEL_CONFLICT"
    assert (
        session.scalar(
            select(func.count())
            .select_from(DiagnosticTree)
            .where(DiagnosticTree.key == value["key"])
        )
        == 2
    )


@pytest.mark.parametrize("case", ["graph", "source", "item", "checksum", "git"])
def test_reimport_and_check_reject_stored_drift(
    session: Session, tmp_path: Path, case: str
) -> None:
    c = corpus(session)
    path = write(tmp_path / "tree.yaml", payload(session, c))
    first = invoke(session, c, path)
    saved = session.get(DiagnosticTree, first.tree_id)
    assert saved
    if case == "graph":
        saved.graph = {**saved.graph, "unexpected": True}
    elif case == "source":
        session.execute(
            text("DELETE FROM app.diag_tree_sources WHERE tree_id=:id"),
            {"id": saved.id},
        )
    elif case == "item":
        session.execute(
            text("UPDATE app.diag_tree_items SET qty=3 WHERE tree_id=:id"),
            {"id": saved.id},
        )
    elif case == "checksum":
        saved.yaml_checksum = "b" * 64
    else:
        saved.git_commit = "b" * 40
    session.flush()
    report = run_tree(
        session, login=c.login, password=PASSWORD, operation="check", tree_id=saved.id
    )
    assert not report.ok
    assert not invoke(session, c, path).ok


@pytest.mark.parametrize(
    ("role", "active", "password", "expected"),
    [
        ("manager", True, PASSWORD, "FORBIDDEN"),
        ("admin", False, PASSWORD, "AUTHENTICATION_DENIED"),
        ("admin", True, "", "AUTHENTICATION_DENIED"),
    ],
)
def test_authorization_before_file_access(
    session: Session,
    tmp_path: Path,
    role: str,
    active: bool,
    password: str,
    expected: str,
) -> None:
    user = admin(session, role=role, active=active)
    report = run_tree(
        session,
        login=user.login,
        password=password,
        operation="import",
        path=tmp_path / "does-not-exist",
        git_commit=COMMIT,
    )
    assert report.code == expected
    anonymous = run_tree(
        session, login="", password="", operation="check", tree_id=uuid4()
    )
    assert anonymous.code == "AUTHENTICATION_DENIED"
    assert not session.scalar(select(DiagnosticTree.id))


@pytest.mark.parametrize("case", ["audit", "links", "commit", "yaml_change"])
def test_failures_rollback_everything(
    database: Database, tmp_path: Path, case: str
) -> None:
    with database.unit_of_work() as session:
        c = corpus(session)
        value = payload(session, c)
    path = write(tmp_path / "tree.yaml", value)
    fired = False

    def fail_sql(
        connection: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        many: bool,
    ) -> None:
        nonlocal fired
        if not fired and (
            (case == "audit" and "INSERT INTO app.audit_events" in statement)
            or (case == "links" and "INSERT INTO app.diag_tree_items" in statement)
        ):
            fired = True
            raise SQLAlchemyError("SYNTHETIC ERROR")

    def fail_commit(session: Session) -> None:
        nonlocal fired
        if not session.in_nested_transaction():
            fired = True
            raise SQLAlchemyError("SYNTHETIC COMMIT ERROR")

    def after_flush(session: Session, context: Any) -> None:
        nonlocal fired
        if fired or not any(
            isinstance(x, AuditEvent)
            and x.action == "tree.import"
            and x.result == "IMPORTED_DRAFT"
            for x in session.new
        ):
            return
        if case == "yaml_change":
            fired = True
            path.write_text(path.read_text() + "\n")

    event.listen(database.engine, "before_cursor_execute", fail_sql)
    if case == "commit":
        event.listen(database.sessions, "before_commit", fail_commit)
    event.listen(database.sessions, "after_flush", after_flush)
    try:
        if case == "yaml_change":
            with database.unit_of_work() as session:
                assert invoke(session, c, path).code == "YAML_CHANGED"
        else:
            with pytest.raises(SQLAlchemyError):
                with database.unit_of_work() as session:
                    invoke(session, c, path)
        assert fired
    finally:
        event.remove(database.engine, "before_cursor_execute", fail_sql)
        if case == "commit":
            event.remove(database.sessions, "before_commit", fail_commit)
        event.remove(database.sessions, "after_flush", after_flush)
    with database.unit_of_work() as session:
        assert not session.scalar(
            select(DiagnosticTree.id).where(DiagnosticTree.key == value["key"])
        )
        assert not session.scalar(
            select(AuditEvent.id).where(
                AuditEvent.entity_type == "diag_tree",
                AuditEvent.result == "IMPORTED_DRAFT",
                AuditEvent.actor_id == c.actor,
            )
        )


def test_actual_postgresql_commit_error(
    databases: tuple[Database, Database], tmp_path: Path
) -> None:
    database, migrator = databases
    with database.unit_of_work() as session:
        c = corpus(session)
        value = payload(session, c)
    with migrator.engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE app.synthetic027_commit (id uuid REFERENCES "
                "app.diag_trees(id) DEFERRABLE INITIALLY DEFERRED)"
            )
        )
    try:
        with pytest.raises(DBAPIError) as caught:
            with database.unit_of_work() as session:
                assert invoke(session, c, write(tmp_path / "tree.yaml", value)).ok
                session.execute(
                    text("INSERT INTO app.synthetic027_commit VALUES (:id)"),
                    {"id": uuid4()},
                )
        assert caught.value.orig.sqlstate == "23503"  # type: ignore[union-attr]
        with database.unit_of_work() as session:
            assert not session.scalar(
                select(DiagnosticTree.id).where(DiagnosticTree.key == value["key"])
            )
            assert not session.scalar(
                select(AuditEvent.id).where(
                    AuditEvent.actor_id == c.actor,
                    AuditEvent.result == "IMPORTED_DRAFT",
                )
            )
    finally:
        with migrator.engine.begin() as connection:
            connection.execute(text("DROP TABLE app.synthetic027_commit"))


@pytest.mark.parametrize("case", ["same", "versions", "model"])
def test_concurrent_imports(database: Database, tmp_path: Path, case: str) -> None:
    with database.unit_of_work() as session:
        c = corpus(session)
        one = payload(session, c)
        two = json.loads(json.dumps(one))
        if case == "versions":
            two["version"] = 2
        if case == "model":
            other = corpus(session)
            two["version"] = 2
            two["model_code"] = session.scalar(
                select(EquipmentModel.code).where(EquipmentModel.id == other.model)
            )
    paths = [write(tmp_path / "one.yaml", one), write(tmp_path / "two.yaml", two)]

    def execute(path: Path) -> str:
        with database.unit_of_work() as session:
            return invoke(session, c, path).code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(execute, paths))
    assert sorted(results) == sorted(
        {
            "same": ["IMPORTED_DRAFT", "NO_OP"],
            "versions": ["IMPORTED_DRAFT", "IMPORTED_DRAFT"],
            "model": ["IMPORTED_DRAFT", "MODEL_CONFLICT"],
        }[case]
    )
    with database.unit_of_work() as session:
        assert session.scalar(
            select(func.count())
            .select_from(DiagnosticTree)
            .where(DiagnosticTree.key == one["key"])
        ) == (2 if case == "versions" else 1)


@pytest.mark.parametrize("case", ["visibility", "delete_source", "item", "disable"])
def test_changes_before_import_recheck(
    database: Database, tmp_path: Path, case: str
) -> None:
    with database.unit_of_work() as session:
        c = corpus(session)
        supervisor = admin(session)
        value = payload(session, c)
        if case == "delete_source":
            # Only draft sections may be deleted by the established KB trigger.
            doc = Document(
                code="SYNTHETIC027-" + uuid4().hex,
                title="SYNTHETIC",
                kind="other",
                visibility="internal",
            )
            session.add(doc)
            session.flush()
            from app.modules.kb.models import DocumentVersion

            version = DocumentVersion(
                document_id=doc.id,
                version=1,
                file_key="Synthetic027",
                mime="text/plain",
                size_bytes=1,
                checksum_sha256="a" * 64,
            )
            session.add(version)
            session.flush()
            section = DocumentSection(
                document_version_id=version.id,
                section_key="test",
                title="SYNTHETIC",
                text="SYNTHETIC",
            )
            session.add(section)
            session.flush()
            value["nodes"]["result"]["source_refs"] = [str(section.id)]
            section_id = section.id
        supervisor_login = supervisor.login
    path = write(tmp_path / "tree.yaml", value)

    def execute() -> TreeReport:
        with database.unit_of_work() as session:
            return invoke(session, c, path)

    with ThreadPoolExecutor(max_workers=1) as pool:
        with database.unit_of_work() as holder:
            if case in {"visibility", "delete_source"}:
                if case == "visibility":
                    holder.execute(
                        text(
                            "UPDATE app.documents SET visibility='internal' "
                            "WHERE id=:id"
                        ),
                        {"id": c.document},
                    )
                else:
                    holder.execute(
                        text("DELETE FROM app.document_sections WHERE id=:id"),
                        {"id": section_id},
                    )
            elif case == "item":
                holder.execute(
                    text("UPDATE app.catalog_items SET status='archived' WHERE id=:id"),
                    {"id": c.item},
                )
            else:
                assert disable_staff(
                    holder,
                    admin_login=supervisor_login,
                    admin_password=PASSWORD,
                    user_id=c.actor,
                ).ok
            future = pool.submit(execute)
            wait_for_lock(database)
        report = future.result(timeout=10)
    if case == "disable":
        assert report.code == "AUTHENTICATION_DENIED"
    elif case == "delete_source":
        assert report.code in {"DEPENDENCIES_CHANGED", "SOURCE_NOT_FOUND"}
    else:
        assert report.code == "IMPORTED_DRAFT"
        assert (
            "SOURCE_NOT_PUBLIC"
            if case == "visibility"
            else "APPLICABILITY_NOT_CONFIRMED"
        ) in {b.code for b in report.blockers}


def test_actual_cli_and_commit_failure(
    database: Database, pipeline: FilePipeline, tmp_path: Path
) -> None:
    with database.unit_of_work() as session:
        c = corpus(session)
        value = payload(session, c)
    path = write(tmp_path / "tree.yaml", value)
    args = ["tree-import", "--file", str(path), "--git-commit", COMMIT]
    status, output = cli_request(pipeline, c.login, args, commit_error=True)
    assert status == 1 and b"IMPORTED_DRAFT" not in output
    assert b"OPERATION_FAILED_ROLLED_BACK" in output
    status, output = cli_request(pipeline, c.login, args)
    assert status == 0 and b"IMPORTED_DRAFT" in output
    with database.unit_of_work() as session:
        identifier = session.scalar(
            select(DiagnosticTree.id).where(DiagnosticTree.key == value["key"])
        )
    status, output = cli_request(
        pipeline, c.login, ["tree-check", "--tree", str(identifier)]
    )
    assert status == 0 and b"STRUCTURALLY_VALID_DRAFT" in output
    assert b"SYNTHETIC choice" not in output


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("version", 0),
        ("key", " "),
        ("title", " "),
        ("yaml_checksum", "xyz"),
        ("git_commit", "main"),
        ("status", "unknown"),
        ("status", "published"),
    ],
)
def test_real_database_checks(
    session: Session, tmp_path: Path, column: str, value: Any
) -> None:
    c = corpus(session)
    report = invoke(session, c, write(tmp_path / "tree.yaml", payload(session, c)))
    with pytest.raises(DBAPIError), session.begin_nested():
        session.execute(
            text(f"UPDATE app.diag_trees SET {column}=:value WHERE id=:id"),
            {"value": value, "id": report.tree_id},
        )


def test_history_links_fks_unique_and_model_guard(
    session: Session, tmp_path: Path
) -> None:
    c = corpus(session)
    value = payload(session, c)
    report = invoke(session, c, write(tmp_path / "tree.yaml", value))
    identifier = report.tree_id
    queries: list[tuple[str, dict[str, Any]]] = [
        ("DELETE FROM app.catalog_items WHERE id=:id", {"id": c.item}),
        ("DELETE FROM app.equipment_models WHERE id=:id", {"id": c.model}),
        (
            "UPDATE app.diag_tree_items SET item_id=:id WHERE tree_id=:tree",
            {"id": uuid4(), "tree": identifier},
        ),
        ("UPDATE app.diag_tree_items SET qty=0 WHERE tree_id=:id", {"id": identifier}),
        (
            "INSERT INTO app.diag_tree_sources SELECT * "
            "FROM app.diag_tree_sources WHERE tree_id=:id",
            {"id": identifier},
        ),
        (
            "INSERT INTO app.diag_trees SELECT * FROM app.diag_trees WHERE id=:id",
            {"id": identifier},
        ),
    ]
    for query, params in queries:
        with pytest.raises(DBAPIError), session.begin_nested():
            session.execute(text(query), params)
    value["version"] = 2
    second = invoke(session, c, write(tmp_path / "tree.yaml", value))
    other = corpus(session)
    with pytest.raises(DBAPIError), session.begin_nested():
        session.execute(
            text(
                "INSERT INTO app.diag_trees "
                "(id,key,version,model_id,title,graph,yaml_checksum,git_commit) "
                "SELECT :id,key,3,:model,title,graph,yaml_checksum,git_commit "
                "FROM app.diag_trees WHERE id=:old"
            ),
            {"id": uuid4(), "model": other.model, "old": identifier},
        )
    # Synthetic DB setup only. Application exposes no publication command.
    approval = {"id": identifier, "actor": c.actor}
    session.execute(
        text(
            "UPDATE app.diag_trees SET status='published', "
            "expert_name='SYNTHETIC ONLY',review_reference='SYNTHETIC:027', "
            "approved_at=now(),published_by=:actor,published_at=now() "
            "WHERE id=:id"
        ),
        approval,
    )
    with pytest.raises(DBAPIError), session.begin_nested():
        session.execute(
            text(
                "UPDATE app.diag_trees SET status='published', "
                "expert_name='SYNTHETIC ONLY',review_reference='SYNTHETIC:027', "
                "approved_at=now(),published_by=:actor,published_at=now() "
                "WHERE id=:id"
            ),
            {"id": second.tree_id, "actor": c.actor},
        )
    for status in ("published", "retired", "revoked"):
        session.execute(
            text("UPDATE app.diag_trees SET status=:status WHERE id=:id"),
            {"id": identifier, "status": status},
        )
        for query in (
            "UPDATE app.diag_trees SET title='CHANGED' WHERE id=:id",
            "UPDATE app.diag_trees SET git_commit=repeat('b',40) WHERE id=:id",
            "UPDATE app.diag_trees SET expert_name='CHANGED' WHERE id=:id",
            "DELETE FROM app.diag_trees WHERE id=:id",
            "UPDATE app.diag_trees SET status='draft' WHERE id=:id",
            "DELETE FROM app.diag_tree_sources WHERE tree_id=:id",
            "UPDATE app.diag_tree_items SET qty=3 WHERE tree_id=:id",
            "INSERT INTO app.diag_tree_sources VALUES (:id,'extra',:section)",
        ):
            with pytest.raises(DBAPIError), session.begin_nested():
                session.execute(text(query), {"id": identifier, "section": c.section})
    with pytest.raises(DBAPIError), session.begin_nested():
        session.execute(
            text("UPDATE app.diag_trees SET status='published' WHERE id=:id"),
            {"id": identifier},
        )
