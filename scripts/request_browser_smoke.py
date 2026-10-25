"""MVP-013 regression and MVP-014 persistence on the same real browser write set.

A bounded line protocol permits fixed synthetic database changes while Chromium
holds an open form. It never accepts arbitrary SQL or an external DB target.
No traces, screenshots, request bodies or raw secrets are written to artifacts.
"""

import hashlib
import json
import subprocess
import threading

from https_smoke import main


def assert_smtp_unavailable(compose):
    for service in ("api", "worker"):
        compose(
            "exec",
            "-T",
            service,
            "python",
            "-c",
            """
import errno,socket
try:
 s=socket.create_connection(('127.0.0.1',25),timeout=2)
except OSError as e:
 assert e.errno == errno.ECONNREFUSED, 'SMTP probe did not get connection refusal'
else:
 s.close()
 raise AssertionError('Acceptance requires SMTP unavailable')
""",
        )


def run_browser(environment: dict, config: dict, compose, stack):
    stack.verify_database()
    assert_smtp_unavailable(compose)
    process = subprocess.Popen(
        ["node", "frontend/tests/request-form.mjs"],
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    # Hard bound prevents an assertion/protocol failure from orphaning the stack.
    timer = threading.Timer(360, process.kill)
    timer.start()
    assert process.stdin and process.stdout and process.stderr

    sql = stack.sql

    def snapshot():
        # Full rows, including IDs, timestamps, snapshots and verification hashes,
        # stay private. Equality must survive restart and all read/replay paths.
        rows = sql(
            "SELECT jsonb_build_array("
            + ",".join(
                f"(SELECT jsonb_agg(to_jsonb(t) ORDER BY id) FROM app.{table} t)"
                for table in (
                    "orders",
                    "consents",
                    "order_items",
                    "order_status_history",
                    "email_outbox",
                )
            )
            + ")"
        )
        return hashlib.sha256(rows.encode()).digest()

    def invariants():
        stack.verify_database()
        # Counts alone cannot prove the rows belong to the same transaction.
        assert (
            sql("""
            SELECT count(*) FROM app.orders o
            JOIN app.consents c ON c.id=o.consent_id
            JOIN app.consent_texts ct ON ct.version=c.text_version
            JOIN app.order_status_history h ON h.order_id=o.id
            JOIN app.email_outbox e ON e.order_id=o.id
            WHERE o.kind='commercial' AND o.source='site' AND o.status='new'
              AND o.version=1 AND o.pii_erased_at IS NULL
              AND o.known_subtotal=246.90 AND o.items_total IS NULL
              AND o.delivery_status='pending_quote' AND o.delivery_cost IS NULL
              AND o.total_amount IS NULL AND o.currency='RUB'
              AND c.text_version='SYNTHETIC-DEMO-ONLY-v1'
              AND c.channel='request_form' AND c.revoked_at IS NULL
              AND h.from_status IS NULL AND h.to_status='new'
              AND h.changed_by IS NULL AND h.reason IS NULL
              AND e.event_kind='manager_notification' AND e.status='pending'
              AND e.attempts=0 AND e.lease_token IS NULL AND e.lease_until IS NULL
              AND e.sent_at IS NULL AND e.last_error_code IS NULL
              AND (SELECT count(*) FROM app.email_outbox)=1
              AND (SELECT count(*) FROM app.order_items i WHERE i.order_id=o.id)=2
              AND EXISTS (SELECT FROM app.order_items i WHERE i.order_id=o.id
                AND i.sku_snapshot='DEMO-P-001' AND i.kind_snapshot='product'
                AND i.qty=2 AND i.price_mode_snapshot='fixed'
                AND i.unit_price_snapshot=123.45 AND i.catalog_revision_snapshot=1)
              AND EXISTS (SELECT FROM app.order_items i WHERE i.order_id=o.id
                AND i.sku_snapshot='DEMO-S-001' AND i.kind_snapshot='service'
                AND i.qty=3 AND i.price_mode_snapshot='on_request'
                AND i.unit_price_snapshot IS NULL AND i.catalog_revision_snapshot=1)
        """)
            == "1"
        ), "Joined PostgreSQL order invariants failed"

    try:
        process.stdin.write(json.dumps(config) + "\n")
        process.stdin.flush()
        proof = None
        original_snapshot = None
        for line in process.stdout:
            event = json.loads(line)
            action = event.get("action")
            if action == "done":
                proof = event["proof"]
                break
            if action == "counts":
                invariants()
                result = [
                    int(v)
                    for v in sql("""
                    SELECT (SELECT count(*) FROM app.orders),
                        (SELECT count(*) FROM app.consents),
                        (SELECT count(*) FROM app.order_items),
                        (SELECT count(*) FROM app.order_status_history),
                        (SELECT count(*) FROM app.email_outbox WHERE status='pending')
                """).split("|")
                ]
                assert result == [1, 1, 2, 1, 1], "Unexpected acceptance write set"
                current = snapshot()
                if original_snapshot is None:
                    original_snapshot = current
                assert current == original_snapshot, "Persisted business rows changed"
            elif action == "restart":
                invariants()
                assert original_snapshot == snapshot(), "Rows changed before stop"
                compose("stop")
                states = [
                    json.loads(line)
                    for line in compose("ps", "--all", "--format", "json").splitlines()
                ]
                assert len(states) == 4 and all(s["State"] == "exited" for s in states)
                stack.verify_volumes()
                compose(
                    "up", "--no-build", "--detach", "--wait", "--wait-timeout", "120"
                )
                invariants()
                assert original_snapshot == snapshot(), (
                    "Rows did not survive Compose stop/up"
                )
                assert_smtp_unavailable(compose)
                result = True
            elif action == "change_consent":
                stack.verify_database()
                # Content is immutable; retirement + a new explicitly synthetic
                # version follows the same publication shape as integration tests.
                sql("""
                    UPDATE app.consent_texts SET retired_at=clock_timestamp()
                        WHERE retired_at IS NULL;
                    INSERT INTO app.consent_texts(version,purpose,text,checksum,active_from)
                    SELECT 'SYNTHETIC-DEMO-ONLY-MVP013-v2',purpose,text,checksum,
                        clock_timestamp() FROM app.consent_texts
                        WHERE version='SYNTHETIC-DEMO-ONLY-v1';
                """)
                result = True
            elif action == "change_catalog":
                stack.verify_database()
                sql(
                    "UPDATE app.catalog_items SET price=150.00, revision=revision+1 WHERE sku='DEMO-P-001'"
                )
                result = True
            elif action == "archive_service":
                stack.verify_database()
                sql(
                    "UPDATE app.catalog_items SET status='archived', revision=revision+1 WHERE sku='DEMO-S-001'"
                )
                result = True
            else:
                raise RuntimeError("Unexpected browser protocol action")
            process.stdin.write(json.dumps(result) + "\n")
            process.stdin.flush()
        process.stdin.close()
        diagnostic = process.stderr.read()
        process.wait(timeout=10)
        return subprocess.CompletedProcess(
            process.args, process.returncode, json.dumps(proof), diagnostic
        )
    finally:
        timer.cancel()
        if process.poll() is None:
            process.kill()
            process.wait()


if __name__ == "__main__":
    main(request_form=True)
