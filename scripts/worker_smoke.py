"""Extend the full guarded API smoke with closed SMTP/worker acceptance."""

import json
import subprocess
import tempfile
from pathlib import Path
from time import monotonic, sleep


def wait_for(predicate, seconds=45):
    deadline = monotonic() + seconds
    while monotonic() < deadline:
        if predicate():
            return
        sleep(0.25)
    raise AssertionError("Worker acceptance deadline exceeded")


def exercise_worker(
    temporary, prefix, compose, sql, inspect, stack, replay, receipt, private
):
    stack.verify_database()
    before = sql(
        "SELECT json_build_object("
        + ",".join(
            f"'{table}', (SELECT json_agg(t ORDER BY id) FROM app.{table} t)"
            for table in ("orders", "consents", "order_items", "order_status_history")
        )
        + ")"
    )
    expected = int(sql("SELECT count(*) FROM app.email_outbox"))
    assert expected == 2
    assert (
        sql("SELECT bool_and(status='pending' AND attempts=0) FROM app.email_outbox")
        == "t"
    )
    root = Path(__file__).resolve().parents[1]
    override = Path(temporary) / "worker.json"
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
                            "PYTHONPATH": "/app",
                            "SMTP_TEST_DOUBLE": "1",
                            "SMTP_HOST": "smtp-test",
                            "SMTP_PORT": "1025",
                            "SMTP_MODE": "test",
                            "SMTP_FROM": "notify@example.invalid",
                            "SMTP_TO": "manager@example.invalid",
                            "SMTP_ADMIN_ORIGIN": "https://staff.example.invalid",
                            "STAFF_TRUSTED_ORIGINS": "https://staff.example.invalid",
                        },
                        "volumes": [
                            str(root / "scripts/worker_probe.py")
                            + ":/test/worker_probe.py:ro"
                        ],
                        "stop_grace_period": "30s",
                    },
                }
            }
        )
    )
    prefix += ["-f", str(override)]
    compose("up", "--no-build", "--detach", "smtp-test")
    compose("up", "--no-build", "--detach", "worker")
    wait_for(
        lambda: (
            sql("SELECT count(*) FROM app.email_outbox WHERE status='sent'")
            == str(expected)
        )
    )
    compose("stop", "worker")
    assert inspect("worker")["State"]["ExitCode"] == 0
    # The probe reads MIME in memory and emits only fixed technical outcomes.
    output = compose(
        "run", "--rm", "--no-deps", "worker", "python", "/test/worker_probe.py"
    )
    assert "PASS: worker wire/retry/timeout/crash/CAS" in output
    print(output, flush=True)
    assert replay() == (200, receipt)

    # Real PostgreSQL outage while the real worker process polls, then resumes.
    stack.verify_database()
    sql(
        "UPDATE app.email_outbox SET status='pending', attempts=0, sent_at=NULL, "
        "next_attempt_at=clock_timestamp() + interval '1 hour' "
        "WHERE id=(SELECT id FROM app.email_outbox ORDER BY id LIMIT 1)"
    )
    compose("start", "worker")
    compose("stop", "postgres")
    wait_for(lambda: "WORKER_TICK_FAILED" in compose("logs", "--no-color", "worker"))
    compose("up", "--no-build", "--detach", "--wait", "postgres")
    stack.verify_database()
    sql(
        "UPDATE app.email_outbox SET next_attempt_at=clock_timestamp() WHERE status='pending'"
    )
    wait_for(
        lambda: (
            sql("SELECT count(*) FROM app.email_outbox WHERE status='sent'")
            == str(expected)
        )
    )
    after = sql("SELECT json_agg(t ORDER BY id) FROM app.email_outbox t")
    compose("stop")
    assert inspect("worker")["State"]["ExitCode"] == 0
    compose("up", "--no-build", "--detach", "--wait", "--wait-timeout", "120")
    stack.verify_database()
    assert sql("SELECT json_agg(t ORDER BY id) FROM app.email_outbox t") == after
    assert before == sql(
        "SELECT json_build_object("
        + ",".join(
            f"'{table}', (SELECT json_agg(t ORDER BY id) FROM app.{table} t)"
            for table in ("orders", "consents", "order_items", "order_status_history")
        )
        + ")"
    ), "Business rows changed"
    assert replay() == (200, receipt)
    logs = compose("logs", "--no-color", "worker", "smtp-test", "api", "postgres")
    for value in private:
        assert value not in logs
    assert "Worker stopped gracefully" in logs
    for service in ("worker", "smtp-test", "api", "postgres"):
        assert not inspect(service)["HostConfig"]["PortBindings"]
    assert inspect("worker")["HostConfig"]["ReadonlyRootfs"]
    assert inspect("worker")["Config"]["User"] == "10001:10001"
    print(
        "PASS: PG outage/recovery, original receipt, immutable rows, stop/up and safe logs",
        flush=True,
    )


def main():
    from compose_smoke import run

    with tempfile.TemporaryDirectory(prefix="akbrizcase-worker-smoke-") as temporary:
        run(temporary, worker_acceptance=True)


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError:
        # Do not echo subprocess output that might contain captured message data.
        raise SystemExit("Worker acceptance subprocess failed") from None
