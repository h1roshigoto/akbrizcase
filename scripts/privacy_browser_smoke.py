"""Verified HTTPS replay and policy availability on an owner-marked test stack."""

import json
import subprocess
import threading
from pathlib import Path

from https_smoke import main

VERSION = "TEST-BROWSER-MVP037-v1"


def operator(compose, directory, passwords, operation):
    script = """
import json,sys
from pathlib import Path
from app.core.db import Database,DatabaseSettings
from app.core.privacy import load_bundle
from app.modules.orders.privacy_operator import run_privacy
from app.modules.orders.seed import DEMO_VERSION
p=json.load(sys.stdin)
d=Database(DatabaseSettings.from_environment(migration=True))
bundle=load_bundle(Path('/run/privacy/approved.json')) if p['operation']=='publish' else None
with d.unit_of_work() as s:
 result=run_privacy(s,login='browser-admin',password=p['password'],operation=p['operation'],
   expected_version=DEMO_VERSION if bundle else 'TEST-BROWSER-MVP037-v1',
   path=Path('/run/privacy/approved.json') if bundle else None,
   expected_checksum=bundle.checksum() if bundle else None)
 assert result.ok, result.code
d.close()
"""
    try:
        compose(
            "run",
            "--rm",
            "-T",
            "-v",
            str(directory) + ":/run/privacy:ro",
            "migrate",
            "python",
            "-c",
            script,
            input_text=json.dumps(
                {"operation": operation, "password": passwords["admin"]}
            ),
        )
    except subprocess.CalledProcessError as error:
        diagnostic = error.stderr or error.stdout or "Fixture operator unavailable"
        for value in passwords.values():
            diagnostic = diagnostic.replace(value, "[redacted]")
        raise RuntimeError(diagnostic) from None


def prepare(compose, stack, directory: Path, passwords):
    stack.verify_database()
    # This is an artificial positive approval record exclusively for testing the
    # real production gate. It is not a customer/legal document or production seed.
    raw = compose(
        "exec",
        "-T",
        "-e",
        "APP_ENV=demo",
        "api",
        "python",
        "-c",
        """
from app.core.privacy import PrivacyBundle,digest
from app.modules.orders.seed import demo_bundle
p=demo_bundle().model_dump(mode='json')
p['approval_status']='APPROVED'
p['approval_ref']='test-fixture-no-external-approval'
p['consent']={'version':'TEST-BROWSER-MVP037-v1','purpose':'TEST FIXTURE ONLY',
 'text':'TEST FIXTURE ONLY. No legal approval.',
 'checksum':digest('TEST FIXTURE ONLY. No legal approval.')}
t='TEST POLICY FIXTURE ONLY. No legal approval.'
p['policy']={'version':'TEST-POLICY-MVP037-v1','text':t,'checksum':digest(t),
 'withdrawal_instructions':'TEST WITHDRAWAL INSTRUCTIONS ONLY. Use the agreed test channel.'}
print(PrivacyBundle.model_validate(p).model_dump_json())
""",
    )
    path = directory / "approved.json"
    path.write_text(raw)
    path.chmod(0o444)
    operator(compose, directory, passwords, "publish")


def run_browser(environment, config, compose, stack, directory, passwords):
    process = subprocess.Popen(
        ["node", "frontend/tests/privacy-lifecycle.mjs"],
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    timer = threading.Timer(240, process.kill)
    timer.start()
    assert process.stdin and process.stdout and process.stderr
    try:
        process.stdin.write(json.dumps(config) + "\n")
        process.stdin.flush()
        proof = None
        for line in process.stdout:
            event = json.loads(line)
            action = event["action"]
            if action == "done":
                proof = event["proof"]
                break
            stack.verify_database()
            if action == "missing_config":
                (directory / "approved.json").rename(directory / "held.json")
            elif action == "restore_config":
                (directory / "held.json").rename(directory / "approved.json")
            elif action == "retire":
                operator(compose, directory, passwords, "retire")
            else:
                raise AssertionError("Unknown privacy acceptance action")
            process.stdin.write("true\n")
            process.stdin.flush()
        process.stdin.close()
        code = process.wait(timeout=20)
        diagnostic = process.stderr.read()
        if code or proof is None:
            raise RuntimeError("Privacy browser acceptance failed: " + diagnostic)
        assert stack.sql("SELECT count(*) FROM app.chat_messages") == "2"
        assert (
            stack.sql(
                "SELECT string_agg(state_version::text,',' ORDER BY id) FROM app.chat_sessions"
            )
            == "1,1"
        )
        return subprocess.CompletedProcess(
            process.args, 0, json.dumps(proof), diagnostic
        )
    finally:
        timer.cancel()
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)


if __name__ == "__main__":
    main(privacy_lifecycle=True)
