"""Exact stable namespace only; admin secrets stay in the CLI's hidden prompts.

This one-shot migrator container never mounts the files volume or supplies its
credentials to API/worker. No seeding, SQL mutations or guard changes.
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path("/home/hiros/.akbriz-front-demo")
PROJECT = "akbriz-staging-3c79ca4d21cd"
HEAD = "a7943888409a798d389bba3f1d382cc2cbc3a549"
CHECKSUM = "31371cd94e0805a7f9f76f9136cc11efc4a8b12817d8767dafc7c79fc321d217"
BUNDLE = (
    Path(__file__).resolve().parents[1] / "docs/mvp-polish-001/privacy.synthetic.json"
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["check", "publish"])
    args = parser.parse_args()
    authority = json.loads((ROOT / "authority.json").read_text())
    manifest = json.loads((ROOT / "first.json").read_text())
    assert authority["project"] == PROJECT
    assert authority["scope"] == "local-synthetic-rehearsal"
    assert manifest["commit"] == HEAD
    inspected = json.loads(
        subprocess.check_output(["docker", "inspect", PROJECT + "-api-1"], text=True)
    )[0]
    assert inspected["Config"]["Labels"]["com.docker.compose.project"] == PROJECT
    assert inspected["Image"] == manifest["backend"]
    command = [
        "docker",
        "run",
        "--rm",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges:true",
        "--user",
        "10001:10001",
        "--memory",
        "256m",
        "--pids-limit",
        "64",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=16m,mode=1777",
    ]
    cli = [
        "python",
        "-m",
        "app.modules.orders.privacy_cli",
        args.operation,
        "--bundle",
        "/operator/privacy.json",
    ]
    command += [
        "--mount",
        f"type=bind,source={BUNDLE},target=/operator/privacy.json,readonly",
    ]
    if args.operation == "publish":
        if not sys.stdin.isatty():
            raise SystemExit(
                "Run publish in a private local terminal; hidden admin input required."
            )
        # Re-read the lifecycle immediately before the operator command. Never
        # guess --expect-empty or overwrite an existing consent edition.
        query = "from app.core.db import Database,DatabaseSettings; from sqlalchemy import text; import json; d=Database(DatabaseSettings.from_environment());\nwith d.unit_of_work() as s: print(json.dumps({'texts':s.scalar(text('select count(*) from app.consent_texts')), 'publications':s.scalar(text('select count(*) from app.consent_publications'))}))\nd.close()"
        state = json.loads(
            subprocess.check_output(
                ["docker", "exec", PROJECT + "-api-1", "python", "-c", query], text=True
            )
        )
        if state != {"texts": 0, "publications": 0}:
            raise SystemExit(
                "STATE_CHANGED: inspect current version and checksum; do not use --expect-empty."
            )
        command += ["-it", "--network", PROJECT + "_data"]
        for name, target in [
            ("migrator_password", "/run/secrets/migrator_password"),
            ("modes.json", "/run/release/modes.json"),
            ("authority.json", "/run/release/authority.json"),
        ]:
            command += [
                "--mount",
                f"type=bind,source={ROOT / name},target={target},readonly",
            ]
        for value in [
            "APP_ENV=staging",
            "DB_HOST=postgres",
            "DB_NAME=akbrizcase",
            "MIGRATION_DB_PASSWORD_FILE=/run/secrets/migrator_password",
            "RELEASE_CONFIG_FILE=/run/release/modes.json",
            "RELEASE_AUTHORITY_FILE=/run/release/authority.json",
            "RELEASE_PROJECT=" + PROJECT,
        ]:
            command += ["-e", value]
        cli += ["--expect-empty", "--checksum", CHECKSUM]
    else:
        command += ["--network", "none"]
    return subprocess.call([*command, manifest["backend"], *cli])


if __name__ == "__main__":
    raise SystemExit(main())
