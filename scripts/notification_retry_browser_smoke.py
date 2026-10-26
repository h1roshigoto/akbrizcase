"""Trusted Chromium → HTTPS → SQLAdmin retry with real closed SMTP and PG."""

import json
import os
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

from https_smoke import main


def run_browser(environment, config, compose, stack, prefix, temporary):
    stack.verify_database()
    compose("stop", "worker")  # deterministic pending observation, isolated stack only
    root = Path(__file__).resolve().parents[1]
    override = Path(temporary) / "retry-smtp.json"
    override.write_text(
        json.dumps(
            {
                "services": {
                    "smtp-test": {
                        "image": "akbrizcase-backend:mvp-003",
                        "pull_policy": "never",
                        "command": ["python", "/test/smtp_double.py"],
                        "networks": ["data"],
                        "read_only": True,
                        "user": "10001:10001",
                        "cap_drop": ["ALL"],
                        "security_opt": ["no-new-privileges:true"],
                        "volumes": [
                            str(root / "backend/tests/integration/smtp_double.py")
                            + ":/test/smtp_double.py:ro"
                        ],
                    },
                    "worker": {
                        "environment": {
                            "APP_ENV": "test",
                            "SMTP_TEST_DOUBLE": "1",
                            "SMTP_HOST": "smtp-test",
                            "SMTP_PORT": "1025",
                            "SMTP_MODE": "test",
                            "SMTP_FROM": "notify@example.invalid",
                            "SMTP_TO": "manager@example.invalid",
                            "SMTP_ADMIN_ORIGIN": config["origin"],
                            "STAFF_TRUSTED_ORIGINS": config["origin"],
                            "PYTHONPATH": "/app:/test",
                        },
                        "volumes": [
                            str(root / ("scripts/" + name)) + ":/test/" + name + ":ro"
                            for name in (
                                "worker_probe.py",
                                "notification_retry_probe.py",
                            )
                        ],
                    },
                }
            }
        )
    )
    prefix += ["-f", str(override)]
    compose("up", "--no-build", "--detach", "smtp-test")
    for service in ("smtp-test", "worker", "api", "postgres"):
        detail = json.loads(compose("ps", "--all", "--format", "json", service) or "{}")
        assert not detail.get("Publishers"), "Private service published a port"

    def probe(operation):
        output = compose(
            "run",
            "--rm",
            "--no-deps",
            "worker",
            "python",
            "/test/notification_retry_probe.py",
            operation,
        )
        result = json.loads(output)
        assert result["result"] == "PASS"
        return result

    def snapshot():
        stack.verify_database()
        return stack.sql(
            "SELECT jsonb_build_array("
            + ",".join(
                f"(SELECT jsonb_agg(to_jsonb(t) ORDER BY id) FROM app.{table} t)"
                for table in (
                    "orders",
                    "order_items",
                    "consents",
                    "order_status_history",
                )
            )
            + ")"
        )

    process = subprocess.Popen(
        ["node", "frontend/tests/notification-retry.mjs"],
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    timer = threading.Timer(180, process.kill)
    timer.start()
    assert process.stdin and process.stdout and process.stderr

    def reply(value):
        process.stdin.write(json.dumps(value) + "\n")
        process.stdin.flush()

    before = identity = None
    try:
        reply(config)
        for line in process.stdout:
            message = json.loads(line)
            action = message["action"]
            if action == "fail":
                before = snapshot()
                result = probe("fail")
                identity = stack.sql(
                    "SELECT jsonb_build_array(id,order_id,event_kind,created_at) FROM app.email_outbox"
                )
                reply(result)
            elif action == "retried":
                assert snapshot() == before, "Retry changed original business rows"
                assert (
                    stack.sql(
                        "SELECT jsonb_build_array(id,order_id,event_kind,created_at) FROM app.email_outbox"
                    )
                    == identity
                )
                assert (
                    stack.sql(
                        "SELECT count(*) FROM app.email_outbox WHERE status='pending' AND attempts=0 AND lease_token IS NULL AND lease_until IS NULL AND last_error_code IS NULL AND sent_at IS NULL AND next_attempt_at <= clock_timestamp()"
                    )
                    == "1"
                )
                assert (
                    stack.sql(
                        "SELECT count(*) FROM app.audit_events a JOIN app.staff_users u ON u.id=a.actor_id JOIN app.email_outbox e ON e.id=a.entity_id WHERE a.action='notification.retry' AND a.entity_type='email_outbox' AND a.result='success' AND u.login='browser-manager' AND a.metadata=jsonb_build_object('from_status','failed','to_status','pending')"
                    )
                    == "1"
                )
                reply({"result": "PASS"})
            elif action == "send":
                reply(probe("send"))
            elif action == "done":
                assert snapshot() == before, "Replay or SMTP changed business rows"
                assert (
                    stack.sql(
                        "SELECT jsonb_build_array(id,order_id,event_kind,created_at) FROM app.email_outbox"
                    )
                    == identity
                )
                assert stack.sql("SELECT count(*) FROM app.email_outbox") == "1"
                assert (
                    stack.sql(
                        "SELECT count(*) FROM app.audit_events WHERE action='notification.retry'"
                    )
                    == "1"
                )
                result = message["proof"]
                result["result"] += "; HEAD=" + os.environ.get(
                    "MVP018_HEAD_SHA",
                    subprocess.check_output(
                        ["git", "rev-parse", "HEAD"], text=True
                    ).strip(),
                )
                reply({"result": "PASS"})
                break
            else:
                raise AssertionError("Unknown fixed browser operation")
        process.stdin.close()
        process.wait(timeout=15)
        diagnostic = process.stderr.read()
        if process.returncode:
            return SimpleNamespace(
                returncode=process.returncode, stdout="", stderr=diagnostic
            )
        return SimpleNamespace(returncode=0, stdout=json.dumps(result), stderr="")
    finally:
        timer.cancel()
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


if __name__ == "__main__":
    main(notification_retry=True)
