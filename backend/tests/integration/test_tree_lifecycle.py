"""MVP-028: real PostgreSQL publication, read guards, rollback and lock races."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, func, select, text
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.orm import Session
from test_catalog_applicability import Corpus, corpus, wait_for_lock
from test_diagnostic_trees import invoke, payload, write
from test_kb_import import PASSWORD, admin
from test_kb_import import pipeline as pipeline
from test_kb_import import session as session
from test_kb_lifecycle import cli_request

from app.core.auth import AuthenticatedStaff
from app.core.db import Database
from app.core.staff_models import AuditEvent, StaffUser
from app.modules.catalog.models import CatalogItemModel
from app.modules.kb.files import FilePipeline
from app.modules.kb.lifecycle_service import (
    LifecycleReport,
    LifecycleService,
    run_lifecycle,
)
from app.modules.kb.tree_lifecycle import Operation, run_tree_lifecycle
from app.modules.kb.tree_models import DiagnosticTree
from app.modules.kb.tree_reads import load_tree

pytestmark = pytest.mark.integration


def prepared(
    session: Session,
    tmp_path: Path,
    *,
    kind: str = "product",
    version: int = 1,
    c: Corpus | None = None,
    key: str | None = None,
) -> tuple[Corpus, UUID, Path, Path]:
    if c is None:
        c = corpus(session, kind=kind)
        now = datetime.now(UTC)
        session.add(
            CatalogItemModel(
                item_id=c.item,
                model_id=c.model,
                evidence_section_id=c.section,
                expert_name="SYNTHETIC ONLY",
                review_reference="SYNTHETIC:028",
                approved_at=now,
                public_allowed=True,
                published_by=c.actor,
                published_at=now,
            )
        )
        session.flush()
    value = payload(session, c)
    value["version"] = version
    if key is not None:
        value["key"] = key
    path = write(tmp_path / (uuid4().hex + ".yaml"), value)
    report = invoke(session, c, path)
    assert report.tree_id and report.ok
    preview = run(session, c, report.tree_id, "preview")
    proof = tmp_path / (uuid4().hex + ".json")
    proof.write_text(
        json.dumps(
            {
                "tree_id": str(report.tree_id),
                "yaml_checksum": preview.technical["yaml_checksum"],
                "binding_sha256": preview.technical["binding_sha256"],
                "expert_name": "SYNTHETIC ONLY Expert",
                "review_reference": "SYNTHETIC:028:REVIEW",
                "approved_at": datetime.now(UTC).isoformat(),
                "decision": "approved",
                "content_checked": True,
            }
        )
    )
    proof.chmod(0o600)
    return c, report.tree_id, path, proof


def run(
    session: Session,
    c: Corpus,
    tree: UUID,
    action: Operation,
    path: Path | None = None,
    proof: Path | None = None,
) -> LifecycleReport:
    return run_tree_lifecycle(
        session,
        login=c.login,
        password=PASSWORD,
        operation=action,
        tree_id=tree,
        path=path,
        evidence_path=proof,
    )


@pytest.mark.parametrize("kind", ["product", "service"])
def test_publish_replace_restore_and_exact_pins(
    session: Session,
    tmp_path: Path,
    kind: str,
) -> None:
    c, one, path, proof = prepared(session, tmp_path, kind=kind)
    assert load_tree(session, tree_id=one) is None
    assert run(session, c, one, "publish", path, proof).code == "PUBLISHED"
    saved = session.get(DiagnosticTree, one)
    assert saved
    key = saved.key
    attribution = (
        saved.expert_name,
        saved.review_reference,
        saved.approved_at,
        saved.published_by,
        saved.published_at,
    )
    assert load_tree(session, key=key).tree_id == one  # type: ignore[union-attr]
    snapshot = load_tree(session, tree_id=one)
    assert snapshot and snapshot.content.nodes["result"].recommendations[
        0
    ].item_id == str(c.item)
    _, two, path2, proof2 = prepared(session, tmp_path, c=c, key=key, version=2)
    assert run(session, c, two, "publish", path2, proof2).ok
    assert saved.status == "retired"
    assert load_tree(session, key=key).tree_id == two  # type: ignore[union-attr]
    assert load_tree(session, tree_id=one).retired  # type: ignore[union-attr]
    assert run(session, c, one, "restore", path).ok
    assert attribution == (
        saved.expert_name,
        saved.review_reference,
        saved.approved_at,
        saved.published_by,
        saved.published_at,
    )
    assert session.get(DiagnosticTree, two).status == "retired"  # type: ignore[union-attr]
    assert run(session, c, one, "publish", path).code == "NO_OP"
    assert run(session, c, one, "revoke").ok
    assert run(session, c, one, "restore", path).code == "TREE_REVOKED"
    assert run(session, c, one, "publish", path, proof).code == "TREE_REVOKED"
    assert load_tree(session, tree_id=one) is None
    assert load_tree(session, key=key) is None
    assert run(session, c, one, "revoke").code == "NO_OP"
    events = list(
        session.scalars(select(AuditEvent).where(AuditEvent.entity_id == one))
    )
    assert {e.action for e in events} >= {"tree.publish", "tree.restore", "tree.revoke"}
    assert all(e.actor_id == c.actor for e in events)
    assert all(PASSWORD not in json.dumps(e.details) for e in events)


@pytest.mark.parametrize(
    "case",
    [
        "missing",
        "future",
        "naive",
        "rejected",
        "unchecked",
        "stale",
        "extra",
        "duplicate",
        "public_file",
    ],
)
def test_expert_evidence_is_required_and_bound(
    session: Session,
    tmp_path: Path,
    case: str,
) -> None:
    c, tree, path, proof = prepared(session, tmp_path)
    data = json.loads(proof.read_text())
    if case == "missing":
        proof_arg = None
    else:
        proof_arg = proof
        if case == "future":
            data["approved_at"] = "2999-01-01T00:00:00+00:00"
        if case == "naive":
            data["approved_at"] = "2020-01-01T00:00:00"
        if case == "rejected":
            data["decision"] = "rejected"
        if case == "unchecked":
            data["content_checked"] = False
        if case == "stale":
            data["binding_sha256"] = "0" * 64
        if case == "extra":
            data["actor_id"] = str(c.actor)
        proof.write_text(json.dumps(data))
        if case == "duplicate":
            proof.write_text('{"decision":"approved",' + proof.read_text()[1:])
        if case == "public_file":
            proof.chmod(0o644)
    assert not run(session, c, tree, "publish", path, proof_arg).ok
    assert session.get(DiagnosticTree, tree).status == "draft"  # type: ignore[union-attr]


@pytest.mark.parametrize(
    "case",
    [
        "internal",
        "revoked",
        "retired",
        "model",
        "item",
        "candidate",
        "changed_yaml",
        "invalid_yaml",
        "stored_graph",
        "stored_link",
    ],
)
def test_publish_revalidates_every_basis(
    session: Session,
    tmp_path: Path,
    case: str,
) -> None:
    c, tree, path, proof = prepared(session, tmp_path)
    if case in {"revoked", "retired"}:
        session.execute(
            text("UPDATE app.document_versions SET status=:status WHERE id=:id"),
            {"status": case, "id": c.version},
        )
    elif case == "internal":
        session.execute(
            text("UPDATE app.documents SET visibility='internal' WHERE id=:id"),
            {"id": c.document},
        )
    elif case == "model":
        session.execute(
            text("UPDATE app.equipment_models SET status='archived' WHERE id=:id"),
            {"id": c.model},
        )
    elif case == "item":
        session.execute(
            text("UPDATE app.catalog_items SET status='archived' WHERE id=:id"),
            {"id": c.item},
        )
    elif case == "candidate":
        session.execute(
            text(
                "UPDATE app.catalog_item_models SET public_allowed=false, "
                "published_by=NULL, published_at=NULL "
                "WHERE item_id=:id"
            ),
            {"id": c.item},
        )
    elif case == "changed_yaml":
        path.write_text(path.read_text() + "\n")
    elif case == "invalid_yaml":
        path.write_text("!!python/object/apply:os.system ['false']")
    elif case == "stored_graph":
        session.execute(
            text("UPDATE app.diag_trees SET graph='{}' WHERE id=:id"), {"id": tree}
        )
    else:
        session.execute(
            text("UPDATE app.diag_tree_items SET qty=3 WHERE tree_id=:id"), {"id": tree}
        )
    assert not run(session, c, tree, "publish", path, proof).ok
    assert session.get(DiagnosticTree, tree).status == "draft"  # type: ignore[union-attr]


@pytest.mark.parametrize("status", ["published", "retired"])
@pytest.mark.parametrize(
    "change",
    [
        "internal",
        "document_revoke",
        "version_revoke",
        "source_retire",
        "item",
        "evidence_retire",
    ],
)
def test_read_guard_rechecks_current_sources_and_recommendations(
    session: Session,
    tmp_path: Path,
    status: str,
    change: str,
) -> None:
    c, tree, path, proof = prepared(session, tmp_path)
    assert run(session, c, tree, "publish", path, proof).ok
    if status == "retired":
        assert run(session, c, tree, "retire").ok
    if change == "internal":
        session.execute(
            text("UPDATE app.documents SET visibility='internal' WHERE id=:id"),
            {"id": c.document},
        )
    elif change == "document_revoke":
        session.execute(
            text("UPDATE app.documents SET status='revoked' WHERE id=:id"),
            {"id": c.document},
        )
    elif change in {"version_revoke", "source_retire", "evidence_retire"}:
        session.execute(
            text("UPDATE app.document_versions SET status=:status WHERE id=:id"),
            {
                "id": c.version,
                "status": "revoked" if change == "version_revoke" else "retired",
            },
        )
    else:
        session.execute(
            text("UPDATE app.catalog_items SET status='archived' WHERE id=:id"),
            {"id": c.item},
        )
    snapshot = load_tree(session, tree_id=tree)
    if change in {"internal", "document_revoke", "version_revoke"}:
        assert snapshot is None
    else:
        assert snapshot
        assert snapshot.content.nodes["result"].recommendations == []
    if change in {"source_retire", "evidence_retire"}:
        assert load_tree(session, key=session.get(DiagnosticTree, tree).key) is None  # type: ignore[union-attr]
        assert not run(
            session, c, tree, "restore" if status == "retired" else "publish", path
        ).ok


def test_document_preview_and_revoke_guard(
    session: Session,
    tmp_path: Path,
    pipeline: FilePipeline,
) -> None:
    c, tree, path, proof = prepared(session, tmp_path)
    assert run(session, c, tree, "publish", path, proof).ok
    report = run_lifecycle(
        session,
        login=c.login,
        password=PASSWORD,
        operation="preview",
        document_id=c.document,
        pipeline=pipeline,
    )
    dependencies = report.technical["dependencies"]
    assert isinstance(dependencies, list)
    assert {
        "kind": "diag_tree_sources.section_id",
        "count": 1,
        "ids": [str(tree)],
    } in dependencies
    assert run_lifecycle(
        session,
        login=c.login,
        password=PASSWORD,
        operation="revoke",
        document_id=c.document,
        pipeline=pipeline,
    ).ok
    assert load_tree(session, tree_id=tree) is None
    assert session.get(DiagnosticTree, tree).status == "published"  # type: ignore[union-attr]


@pytest.mark.parametrize("role,active", [("manager", True), ("admin", False)])
@pytest.mark.parametrize(
    "action", ["publish", "restore", "revoke", "retire", "preview"]
)
def test_authorization_before_io(
    session: Session,
    tmp_path: Path,
    role: str,
    active: bool,
    action: Operation,
) -> None:
    actor = admin(session, role=role, active=active)
    result = run_tree_lifecycle(
        session,
        login=actor.login,
        password=PASSWORD,
        operation=action,
        tree_id=uuid4(),
        path=tmp_path / "missing",
        evidence_path=tmp_path / "missing",
    )
    assert result.code == ("FORBIDDEN" if active else "AUTHENTICATION_DENIED")


@pytest.mark.parametrize("case", ["yaml_change", "proof_change", "audit", "commit"])
def test_replacement_rollback_includes_retirement_and_audit(
    database: Database,
    tmp_path: Path,
    case: str,
) -> None:
    with database.unit_of_work() as s:
        c, one, path, proof = prepared(s, tmp_path)
        assert run(s, c, one, "publish", path, proof).ok
        key = s.get(DiagnosticTree, one).key  # type: ignore[union-attr]
        _, two, path2, proof2 = prepared(s, tmp_path, c=c, key=key, version=2)
    fired = False

    def after_flush(s: Session, context: Any) -> None:
        nonlocal fired
        if fired or not any(
            isinstance(e, AuditEvent) and e.entity_id == two and e.result == "PUBLISHED"
            for e in s.new
        ):
            return
        fired = True
        if case == "yaml_change":
            path2.write_text(path2.read_text() + "\n")
        if case == "proof_change":
            proof2.write_text(proof2.read_text() + "\n")
        if case == "audit":
            raise SQLAlchemyError("SYNTHETIC audit failure")

    def before_commit(s: Session) -> None:
        if not s.in_nested_transaction():
            raise SQLAlchemyError("SYNTHETIC commit failure")

    event.listen(database.sessions, "after_flush", after_flush)
    if case == "commit":
        event.listen(database.sessions, "before_commit", before_commit)
    try:
        if case in {"audit", "commit"}:
            with pytest.raises(SQLAlchemyError), database.unit_of_work() as s:
                run(s, c, two, "publish", path2, proof2)
        else:
            with database.unit_of_work() as s:
                assert run(s, c, two, "publish", path2, proof2).code == (
                    "YAML_CHANGED" if case == "yaml_change" else "MANIFEST_CHANGED"
                )
    finally:
        event.remove(database.sessions, "after_flush", after_flush)
        if case == "commit":
            event.remove(database.sessions, "before_commit", before_commit)
    with database.unit_of_work() as s:
        assert s.get(DiagnosticTree, one).status == "published"  # type: ignore[union-attr]
        assert s.get(DiagnosticTree, two).status == "draft"  # type: ignore[union-attr]
        assert not s.scalar(
            select(AuditEvent.id).where(
                AuditEvent.entity_id == two, AuditEvent.result == "PUBLISHED"
            )
        )


@pytest.mark.parametrize("same", [True, False])
def test_concurrent_publications(
    database: Database, tmp_path: Path, same: bool
) -> None:
    with database.unit_of_work() as s:
        c, one, path, proof = prepared(s, tmp_path)
        key = s.get(DiagnosticTree, one).key  # type: ignore[union-attr]
        _, two, path2, proof2 = prepared(s, tmp_path, c=c, key=key, version=2)
    jobs = [(one, path, proof), (one, path, None) if same else (two, path2, proof2)]

    def execute(job: tuple[UUID, Path, Path | None]) -> str:
        with database.unit_of_work() as s:
            return run(s, c, job[0], "publish", job[1], job[2]).code

    # Both duplicate attempts need evidence if the second wins the staff lock.
    if same:
        jobs[1] = jobs[0]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(execute, jobs))
    if same:
        # A repeated review cannot mutate historical approval.
        assert sorted(results) == ["NO_OP", "PUBLISHED"]
    else:
        assert results == ["PUBLISHED", "PUBLISHED"]
    with database.unit_of_work() as s:
        assert (
            s.scalar(
                select(func.count())
                .select_from(DiagnosticTree)
                .where(DiagnosticTree.key == key, DiagnosticTree.status == "published")
            )
            == 1
        )


@pytest.mark.parametrize("transition", ["revoke", "visibility"])
def test_kb_lifecycle_never_waits_on_tree_key(
    database: Database,
    tmp_path: Path,
    pipeline: FilePipeline,
    transition: Literal["revoke", "visibility"],
) -> None:
    with database.unit_of_work() as s:
        c, tree, path, proof = prepared(s, tmp_path)
        assert run(s, c, tree, "publish", path, proof).ok
        key = s.get(DiagnosticTree, tree).key  # type: ignore[union-attr]
    # Deliberately exercise the lock graph beneath global CLI staff serialization:
    # a key holder waits on document; the document holder executes the real KB
    # service and dependency provider while that key remains held.
    with ThreadPoolExecutor(max_workers=1) as pool:
        with database.unit_of_work() as holder:
            holder.execute(
                text("SELECT id FROM app.documents WHERE id=:id FOR UPDATE"),
                {"id": c.document},
            )
            user = holder.get(StaffUser, c.actor)
            assert user

            def key_holder() -> str:
                with database.unit_of_work() as worker:
                    worker.execute(
                        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                        {"key": "diag-tree:" + key},
                    )
                    worker.execute(
                        text("SELECT id FROM app.documents WHERE id=:id FOR UPDATE"),
                        {"id": c.document},
                    )
                    return "completed"

            pending = pool.submit(key_holder)
            wait_for_lock(database)
            report = LifecycleService(
                holder, AuthenticatedStaff(user), pipeline
            ).execute(
                transition,
                c.document,
                visibility="internal" if transition == "visibility" else None,
            )
            assert report.ok
        assert pending.result(timeout=10) == "completed"
    with database.unit_of_work() as s:
        assert load_tree(s, tree_id=tree) is None


def test_publish_waits_then_rechecks_document(
    database: Database, tmp_path: Path
) -> None:
    with database.unit_of_work() as s:
        c, tree, path, proof = prepared(s, tmp_path)

    def publish() -> str:
        with database.unit_of_work() as s:
            return run(s, c, tree, "publish", path, proof).code

    with ThreadPoolExecutor(max_workers=1) as pool:
        with database.unit_of_work() as holder:
            holder.execute(
                text("UPDATE app.documents SET visibility='internal' WHERE id=:id"),
                {"id": c.document},
            )
            pending = pool.submit(publish)
            wait_for_lock(database)
        assert pending.result(timeout=10) == "PUBLICATION_BLOCKED"


def test_real_cli_publication_and_revoke(
    database: Database, tmp_path: Path, pipeline: FilePipeline
) -> None:
    with database.unit_of_work() as s:
        c, tree, path, proof = prepared(s, tmp_path)
    arguments = [
        "tree-publish",
        "--tree",
        str(tree),
        "--file",
        str(path),
        "--evidence",
        str(proof),
    ]
    code, output = cli_request(pipeline, c.login, arguments, commit_error=True)
    assert code == 1 and b"OPERATION_FAILED_ROLLED_BACK" in output
    assert b'"code": "PUBLISHED"' not in output
    with database.unit_of_work() as s:
        assert s.get(DiagnosticTree, tree).status == "draft"  # type: ignore[union-attr]
    code, output = cli_request(
        pipeline,
        c.login,
        [
            "tree-publish",
            "--tree",
            str(tree),
            "--file",
            str(path),
            "--evidence",
            str(proof),
        ],
    )
    assert code == 0 and b'"code": "PUBLISHED"' in output
    code, output = cli_request(pipeline, c.login, ["tree-revoke", "--tree", str(tree)])
    assert code == 0 and b'"code": "REVOKED"' in output
    assert PASSWORD.encode() not in output


@pytest.mark.parametrize(
    "change", ["graph", "sources", "items", "review", "model", "revoked_restore"]
)
def test_published_history_database_guards(
    session: Session, tmp_path: Path, change: str
) -> None:
    c, tree, path, proof = prepared(session, tmp_path)
    assert run(session, c, tree, "publish", path, proof).ok
    statements = {
        "graph": "UPDATE app.diag_trees SET graph='{}' WHERE id=:id",
        "sources": "DELETE FROM app.diag_tree_sources WHERE tree_id=:id",
        "items": "UPDATE app.diag_tree_items SET qty=3 WHERE tree_id=:id",
        "review": (
            "UPDATE app.diag_trees SET expert_name='SYNTHETIC OTHER' WHERE id=:id"
        ),
        "model": "UPDATE app.diag_trees SET model_id=:model WHERE id=:id",
        "revoked_restore": "UPDATE app.diag_trees SET status='published' WHERE id=:id",
    }
    other = corpus(session)
    if change == "revoked_restore":
        assert run(session, c, tree, "revoke").ok
    with pytest.raises(DBAPIError), session.begin_nested():
        session.execute(text(statements[change]), {"id": tree, "model": other.model})


def test_revoke_draft_with_invalid_graph_does_not_require_dependencies(
    session: Session, tmp_path: Path
) -> None:
    c, tree, _, _ = prepared(session, tmp_path)
    session.execute(
        text("UPDATE app.diag_trees SET graph='{}' WHERE id=:id"), {"id": tree}
    )
    assert run(session, c, tree, "revoke").ok


def test_recommendations_correlate_to_exact_model(
    session: Session, tmp_path: Path
) -> None:
    c, tree, path, proof = prepared(session, tmp_path)
    other, other_tree, other_path, other_proof = prepared(session, tmp_path)
    assert run(session, c, tree, "publish", path, proof).ok
    assert run(session, other, other_tree, "publish", other_path, other_proof).ok
    session.execute(
        text(
            "UPDATE app.catalog_item_models SET public_allowed=false, "
            "published_by=NULL, published_at=NULL WHERE item_id=:id"
        ),
        {"id": c.item},
    )
    one, two = load_tree(session, tree_id=tree), load_tree(session, tree_id=other_tree)
    assert one and one.content.nodes["result"].recommendations == []
    assert two and len(two.content.nodes["result"].recommendations) == 1


def test_restore_and_revoke_serialize(database: Database, tmp_path: Path) -> None:
    with database.unit_of_work() as s:
        c, tree, path, proof = prepared(s, tmp_path)
        assert run(s, c, tree, "publish", path, proof).ok
        assert run(s, c, tree, "retire").ok

    def execute(action: Operation) -> str:
        with database.unit_of_work() as s:
            return run(s, c, tree, action, path if action == "restore" else None).code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(execute, ["restore", "revoke"]))
    assert results[0] in {"PUBLISHED", "TREE_REVOKED"}
    assert results[1] == "REVOKED"
    with database.unit_of_work() as s:
        assert s.get(DiagnosticTree, tree).status == "revoked"  # type: ignore[union-attr]
        assert load_tree(s, tree_id=tree) is None
