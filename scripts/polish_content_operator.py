"""Authenticated, non-seeding SYNTHETIC ONLY content onboarding for local review.

Run from a private terminal. No passwords in argv, environment, pipes or files.
Existing documents, trees, sessions, items and requests are never reset.
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

TEXT = (
    "SYNTHETIC ONLY. Учебная проверка МТД-КА-01. Это демонстрация интерфейса, "
    "а не инструкция производителя. Посмотрите только на учебный экран: видна ли надпись DEMO? "
    "Если надпись видна, учебная проверка завершена. Если надписи нет или есть сомнения, "
    "обратитесь к специалисту через форму. Не выполняйте никаких действий с реальным оборудованием."
)
KEY = "SYNTHETIC-POLISH-001"


def onboard(login, password, *, commit):
    """Use the exact import/check/review/publish services behind the protected CLI."""
    import tempfile
    from datetime import UTC, datetime

    from app.core.auth import has_role
    from app.core.db import Database, DatabaseSettings
    from app.core.release import full_synthetic_database
    from app.core.staff import authenticate_operator
    from app.modules.catalog.models import EquipmentModel
    from app.modules.kb.files import FilePipeline
    from app.modules.kb.import_service import run_preparation
    from app.modules.kb.lifecycle_service import run_lifecycle
    from app.modules.kb.models import Document, DocumentSection, DocumentVersion
    from app.modules.kb.tree_lifecycle import run_tree_lifecycle
    from app.modules.kb.tree_service import run_tree
    from sqlalchemy import select

    db = Database(DatabaseSettings.from_environment())
    pipeline = FilePipeline.from_environment()

    def require(report):
        if not report.ok:
            raise ValueError(report.code)
        return report

    with tempfile.TemporaryDirectory(prefix="polish-content-") as temporary:
        root = Path(temporary)

        def record(name, data):
            path = root / name
            path.write_text(json.dumps(data, ensure_ascii=False))
            path.chmod(0o600)
            return path

        # Each successful stage is committed separately, preserving normal CLI
        # recovery and audit semantics if a later stage fails.
        with db.unit_of_work() as s:
            assert full_synthetic_database(s)
            actor = authenticate_operator(s, login, password)
            assert actor and has_role(actor.user, "admin"), "AUTHENTICATION_REQUIRED"
            model = s.scalar(
                select(EquipmentModel).where(
                    EquipmentModel.code == "MTD-KA-01",
                    EquipmentModel.status == "published",
                )
            )
            assert model, "PUBLISHED_MTD_KA_01_REQUIRED"
            model_code = model.code
            existing = s.scalar(select(Document).where(Document.code == KEY))
            if existing:
                raise ValueError(
                    "CONTENT_EXISTS: use authenticated preview; no automatic overwrite or reseed"
                )
        source = root / "synthetic.txt"
        source.write_text(TEXT + "\n")
        manifest = record(
            "import.json",
            {
                "code": KEY,
                "title": "SYNTHETIC ONLY — учебная проверка МТД-КА-01",
                "kind": "other",
                "visibility": "public",
                "version": 1,
                "checksum_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "preparation": "manual",
                "sections": [
                    {
                        "section_key": "demo-observation",
                        "title": "Учебная проверка экрана DEMO",
                        "text": TEXT,
                        "assistant_allowed": True,
                    }
                ],
            },
        )
        for action in ("import", "check"):
            with db.unit_of_work() as s:
                require(
                    run_preparation(
                        s,
                        login=login,
                        password=password,
                        manifest_path=manifest,
                        source_path=source if action == "import" else None,
                        pipeline=pipeline,
                    )
                )
            print("KB_" + action.upper() + "_SUCCESS", flush=True)
        with db.unit_of_work() as s:
            doc = s.scalar(select(Document).where(Document.code == KEY))
            version = s.scalar(
                select(DocumentVersion).where(
                    DocumentVersion.document_id == doc.id, DocumentVersion.version == 1
                )
            )
            doc_id, version_id = doc.id, version.id
            section_id = s.scalar(
                select(DocumentSection.id).where(
                    DocumentSection.document_version_id == version_id
                )
            )
            preview = require(
                run_lifecycle(
                    s,
                    login=login,
                    password=password,
                    operation="preview",
                    document_id=doc_id,
                    pipeline=pipeline,
                )
            )
        binding = next(
            v
            for v in preview.technical["versions"]
            if v["version_id"] == str(version_id)
        )
        evidence = record(
            "review.json",
            {
                **{
                    k: binding[k]
                    for k in (
                        "version_id",
                        "checksum_sha256",
                        "file_binding_sha256",
                        "sections_sha256",
                    )
                },
                "expert_name": "SYNTHETIC ONLY local demo reviewer",
                "expert_reference": "SYNTHETIC:POLISH001:LOCAL-REVIEW",
                "expert_approved_on": datetime.now(UTC).date().isoformat(),
                "decision": "approved",
                "content_checked": True,
                "full_file_public_allowed": True,
                "public_release_reference": "SYNTHETIC:POLISH001:LOCAL-PUBLIC",
            },
        )
        for action in ("review", "publish"):
            with db.unit_of_work() as s:
                require(
                    run_lifecycle(
                        s,
                        login=login,
                        password=password,
                        operation=action,
                        document_id=doc_id,
                        version_id=version_id,
                        pipeline=pipeline,
                        **({"evidence_path": evidence} if action == "review" else {}),
                    )
                )
            print("KB_" + action.upper() + "_SUCCESS", flush=True)
        tree = record(
            "tree.yaml",
            {
                "key": KEY,
                "version": 1,
                "model_code": model_code,
                "title": "SYNTHETIC ONLY — видна ли надпись DEMO?",
                "start": "question",
                "nodes": {
                    "question": {
                        "type": "question",
                        "risk": "observation",
                        "text": "SYNTHETIC ONLY. Учебная проверка: на учебном экране видна надпись DEMO? Не выполняйте действий с реальным оборудованием.",
                        "source_refs": [str(section_id)],
                        "options": [
                            {
                                "id": "visible",
                                "text": "Да, вижу DEMO на учебном экране",
                                "next": "result",
                            },
                            {
                                "id": "uncertain",
                                "text": "Нет или не уверен — обратиться к специалисту",
                                "next": "handoff",
                            },
                        ],
                    },
                    "result": {
                        "type": "result",
                        "risk": "observation",
                        "text": "SYNTHETIC ONLY. Учебная проверка завершена. Это демонстрация интерфейса, а не заключение о состоянии оборудования.",
                        "source_refs": [str(section_id)],
                    },
                    "handoff": {
                        "type": "handoff",
                        "risk": "expert_only",
                        "text": "Обратитесь к специалисту через форму. Не выполняйте действий с реальным оборудованием.",
                    },
                },
            },
        )
        with db.unit_of_work() as s:
            require(
                run_tree(
                    s, login=login, password=password, operation="check", path=tree
                )
            )
        with db.unit_of_work() as s:
            imported = require(
                run_tree(
                    s,
                    login=login,
                    password=password,
                    operation="import",
                    path=tree,
                    git_commit=commit,
                )
            )
            tree_id = imported.tree_id
        with db.unit_of_work() as s:
            preview = require(
                run_tree_lifecycle(
                    s,
                    login=login,
                    password=password,
                    operation="preview",
                    tree_id=tree_id,
                )
            )
        proof = record(
            "tree-review.json",
            {
                "tree_id": str(tree_id),
                "yaml_checksum": preview.technical["yaml_checksum"],
                "binding_sha256": preview.technical["binding_sha256"],
                "expert_name": "SYNTHETIC ONLY local demo reviewer",
                "review_reference": "SYNTHETIC:POLISH001:LOCAL-REVIEW",
                "approved_at": datetime.now(UTC).isoformat(),
                "decision": "approved",
                "content_checked": True,
            },
        )
        with db.unit_of_work() as s:
            require(
                run_tree_lifecycle(
                    s,
                    login=login,
                    password=password,
                    operation="publish",
                    tree_id=tree_id,
                    path=tree,
                    evidence_path=proof,
                )
            )
        print(
            json.dumps(
                {
                    "code": "CONTENT_PUBLISHED_SYNTHETIC_ONLY",
                    "document_id": str(doc_id),
                    "version_id": str(version_id),
                    "tree_id": str(tree_id),
                }
            )
        )
    db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inside", action="store_true")
    parser.add_argument("--project", default="akbriz-staging-3c79ca4d21cd")
    parser.add_argument(
        "--root", type=Path, default=Path("/home/hiros/.akbriz-front-demo")
    )
    args = parser.parse_args()
    if args.inside:
        from getpass import getpass

        if not sys.stdin.isatty():
            raise SystemExit("SECURE_TERMINAL_REQUIRED")
        print(TEXT, flush=True)
        print(
            "Path A: DEMO visible → synthetic result → feedback. Path B: uncertain → specialist. No technical repairs or recommendations.\nReview applies ONLY to this synthetic local training document and both paths.",
            flush=True,
        )
        if (
            input(
                "Type SYNTHETIC ONLY to approve this local training review/publication: "
            )
            != "SYNTHETIC ONLY"
        ):
            raise SystemExit("NOT_APPROVED")
        login = getpass("Acting admin login: ")
        password = getpass("Acting admin password: ")
        try:
            onboard(login, password, commit=os.environ["POLISH_SOURCE_COMMIT"])
        except Exception as error:  # noqa: BLE001
            # CLI boundary: never print exception values, SQL or credentials.
            print(
                "CONTENT_BLOCKED "
                + type(error).__name__
                + "; inspect authenticated preview before retry.",
                file=sys.stderr,
            )
            return 1
        return 0
    authority = json.loads((args.root / "authority.json").read_text())
    assert (
        authority["scope"] == "local-synthetic-rehearsal"
        and authority["project"] == args.project
    )
    assert authority["profile"] == "full-synthetic-staging"
    manifest = json.loads((args.root / "first.json").read_text())
    api = json.loads(
        subprocess.check_output(
            ["docker", "inspect", args.project + "-api-1"], text=True
        )
    )[0]
    assert (
        api["Image"] == manifest["backend"]
        and api["Config"]["Labels"]["com.docker.compose.project"] == args.project
    )
    signed = Path("/home/hiros/demo-import/DEMO-001-private/signed-cvd")
    assert {p.name for p in signed.iterdir()} == {
        "main.cvd",
        "daily.cvd",
        "bytecode.cvd",
    }
    cmd = [
        "docker",
        "run",
        "--rm",
        "-it",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges:true",
        "--user",
        "10001:10001",
        "--memory",
        "2g",
        "--cpus",
        "1",
        "--pids-limit",
        "64",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=96m,mode=1777",
        "--network",
        args.project + "_data",
    ]
    for src, target in [
        (Path(__file__).resolve(), "/operator.py"),
        (args.root / "authority.json", "/run/release/authority.json"),
        (args.root / "modes.json", "/run/release/modes.json"),
        (args.root / "runtime_password", "/run/secrets/runtime_password"),
        (signed, "/var/lib/akbrizcase/clamav"),
    ]:
        cmd += ["--mount", f"type=bind,source={src},target={target},readonly"]
    cmd += [
        "--mount",
        f"type=volume,source={args.project}_files,target=/var/lib/akbrizcase/files",
    ]
    for setting in [
        "PYTHONPATH=/app",
        "APP_ENV=staging",
        "DB_HOST=postgres",
        "DB_NAME=akbrizcase",
        "DB_PASSWORD_FILE=/run/secrets/runtime_password",
        "APP_FILES_DIR=/var/lib/akbrizcase/files",
        "RELEASE_PROJECT=" + args.project,
        "RELEASE_CONFIG_FILE=/run/release/modes.json",
        "RELEASE_AUTHORITY_FILE=/run/release/authority.json",
        "POLISH_SOURCE_COMMIT=" + manifest["commit"],
    ]:
        cmd += ["-e", setting]
    return subprocess.call(
        [*cmd, manifest["backend"], "python", "/operator.py", "--inside"]
    )


if __name__ == "__main__":
    raise SystemExit(main())
