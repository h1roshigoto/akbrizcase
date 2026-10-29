"""MVP-013 regression and MVP-014 persistence on the same real browser write set.

A bounded line protocol permits fixed synthetic database changes while Chromium
holds an open form. It never accepts arbitrary SQL or an external DB target.
No traces, screenshots, request bodies or raw secrets are written to artifacts.
"""

import hashlib
import json
import re
import subprocess
import sys
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

    def status_snapshot():
        # Compare every immutable field, including verification material, privately.
        return sql("""
          SELECT jsonb_build_array(
            (SELECT jsonb_agg(to_jsonb(o) - ARRAY['status','version','updated_at','closed_at'] ORDER BY id) FROM app.orders o),
            (SELECT jsonb_agg(to_jsonb(c) ORDER BY id) FROM app.consents c),
            (SELECT jsonb_agg(to_jsonb(i) ORDER BY id) FROM app.order_items i),
            (SELECT jsonb_agg(to_jsonb(h) ORDER BY id) FROM app.order_status_history h WHERE from_status IS NULL),
            (SELECT jsonb_agg(to_jsonb(e) ORDER BY id) FROM app.email_outbox e))
        """)

    def status_invariants(version):
        stack.verify_database()
        target = {2: "in_progress", 3: "closed"}[version]
        assert (
            sql(f"""
          SELECT count(*) FROM app.orders o WHERE o.status='{target}' AND o.version={version}
            AND (SELECT count(*) FROM app.order_status_history)={version}
            AND (SELECT count(*) FROM app.audit_events WHERE action='order.status.change')={version - 1}
            AND (o.closed_at IS NOT NULL)={"true" if version == 3 else "false"}
            AND EXISTS (SELECT FROM app.order_status_history h
              JOIN app.staff_users u ON u.id=h.changed_by
              JOIN app.audit_events a ON a.entity_id=o.id AND a.actor_id=u.id
              WHERE h.order_id=o.id AND u.login='browser-manager'
                AND h.to_status='{target}' AND h.at=o.updated_at AND a.at=h.at
                AND a.action='order.status.change' AND a.entity_type='order' AND a.result='success'
                AND a.metadata=jsonb_build_object('from_status',h.from_status,'to_status',h.to_status,
                  'from_version',{version - 1},'to_version',{version}))
        """)
            == "1"
        ), "PostgreSQL status/history/audit invariants failed"

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

    def counts():
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
        return dict(
            zip(
                (
                    "orders",
                    "consents",
                    "order_items",
                    "initial_history",
                    "pending_outbox",
                ),
                result,
                strict=True,
            )
        )

    try:
        process.stdin.write(json.dumps(config) + "\n")
        process.stdin.flush()
        proof = None
        original_snapshot = None
        restart_counts = {}
        status_baseline = None
        status_proof = None
        for line in process.stdout:
            event = json.loads(line)
            action = event.get("action")
            if action == "done":
                proof = event["proof"]
                checkout = subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip()
                head = environment.get("MVP014_HEAD_SHA", checkout)
                assert re.fullmatch(r"[0-9a-f]{40}", head)
                assert set(restart_counts) == {"before", "after"}
                proof["acceptance"] = {
                    "head_sha": head,
                    "checkout_sha": checkout,
                    "postgresql_version": sql("SHOW server_version"),
                    "alembic_revision": sql(
                        "SELECT version_num FROM app.alembic_version"
                    ),
                    "receipt": proof["receipt"],
                    "restart_counts": restart_counts,
                    "post_restart_replay": 200,
                    "manager_reload": "passed",
                    "smtp": "connection_refused",
                    "business_rows_unchanged": True,
                }
                if config.get("status_changes"):
                    assert status_proof is not None
                    proof["status_acceptance"] = status_proof
                break
            if action == "counts":
                invariants()
                result = list(counts().values())
                current = snapshot()
                if original_snapshot is None:
                    original_snapshot = current
                assert current == original_snapshot, "Persisted business rows changed"
            elif action == "restart":
                invariants()
                restart_counts["before"] = counts()
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
                restart_counts["after"] = counts()
                assert original_snapshot == snapshot(), (
                    "Rows did not survive Compose stop/up"
                )
                assert_smtp_unavailable(compose)
                result = True
            elif action == "change_consent":
                stack.verify_database()
                # Content is immutable; retirement + a new explicitly synthetic
                # version follows the same publication shape as integration tests.
                compose("exec", "-T", "api", "python", "-c", """
from app.core.db import Database, DatabaseSettings
from app.core.privacy import PrivacyBundle
from app.core.db_types import utc_now
from app.modules.orders.models import ConsentText
from app.modules.orders.privacy_models import ConsentPublication
from app.modules.orders.privacy_service import lock_privacy
from app.modules.orders.seed import demo_bundle, DEMO_VERSION
from sqlalchemy import select
# Restricted synthetic acceptance fixture; owner marker checked by the harness.
d = Database(DatabaseSettings.from_environment())
with d.unit_of_work() as s:
 lock_privacy(s, shared=False)
 old=s.get(ConsentText, DEMO_VERSION)
 assert old is not None
 old.retired_at=utc_now()
 data=demo_bundle().model_dump(mode='json')
 data['consent']['version']='SYNTHETIC-DEMO-ONLY-MVP013-v2'
 bundle=PrivacyBundle.model_validate(data)
 s.add(ConsentText(**bundle.consent.model_dump(),active_from=utc_now()))
 s.flush()
 s.add(ConsentPublication(consent_version=bundle.consent.version,
   bundle_checksum=bundle.checksum(),bundle=bundle.model_dump(mode='json'),
   published_at=utc_now()))
d.close()
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
            elif action == "status_baseline":
                invariants()
                counts()
                status_baseline = status_snapshot()
                assert (
                    sql(
                        "SELECT count(*) FROM app.audit_events WHERE action='order.status.change'"
                    )
                    == "0"
                )
                result = True
            elif action in ("status_progress", "status_complete", "status_replayed"):
                assert config.get("status_changes") and status_baseline is not None
                version = 2 if action == "status_progress" else 3
                status_invariants(version)
                assert status_baseline == status_snapshot(), (
                    "Immutable business rows changed"
                )
                if action == "status_complete":
                    completed_snapshot = snapshot()
                    completed_audit = sql(
                        "SELECT jsonb_agg(to_jsonb(a) ORDER BY id) FROM app.audit_events a WHERE action='order.status.change'"
                    )
                if action == "status_replayed":
                    assert completed_snapshot == snapshot(), (
                        "Replay changed business state"
                    )
                    assert completed_audit == sql(
                        "SELECT jsonb_agg(to_jsonb(a) ORDER BY id) FROM app.audit_events a WHERE action='order.status.change'"
                    )
                    status_proof = {
                        "head_sha": environment.get(
                            "MVP015_HEAD_SHA",
                            subprocess.check_output(
                                ["git", "rev-parse", "HEAD"], text=True
                            ).strip(),
                        ),
                        "transitions": ["new -> in_progress", "in_progress -> closed"],
                        "versions": [1, 2, 3],
                        "history": 3,
                        "order_audit": 2,
                        "actor": "browser-manager",
                        "stale_form": 409,
                        "immutable_rows_unchanged": True,
                        "original_receipt_replay": 200,
                        "trusted_https": True,
                    }
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
    if sys.argv[1:] not in ([], ["--status"]):
        raise SystemExit("Usage: request_browser_smoke.py [--status]")
    main(request_form=True, status_changes=sys.argv[1:] == ["--status"])
