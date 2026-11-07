"""One linked browser/write set, fixed synthetic transitions, safe evidence only."""

import hashlib
import json
import os
import subprocess
import threading
from pathlib import Path
from time import monotonic
from uuid import UUID

from https_smoke import main
from notification_retry_browser_smoke import prepare_smtp

CASES = {
    "A.desktop",
    "A.mobile",
    "B.desktop",
    "B.mobile",
    "C.desktop",
    "C.mobile",
    "D.images",
    "D.applicability",
    "E.409",
    "E.410",
    "E.429",
    "E.network-before",
    "E.network-after",
    "keyboard.desktop",
    "keyboard.mobile",
}


def validate_result(checkpoints, counts, numbers):
    assert set(checkpoints) == CASES and len(checkpoints) == len(CASES), (
        "Missing or duplicate mandatory journey"
    )
    assert counts == [5, 5, 4, 5, 7], "Unexpected final writes"
    assert len(numbers) == len(set(numbers)) == 5, "Duplicate or missing receipt"


def run_browser(environment, config, compose, stack, prefix, temporary):
    started = monotonic()
    checkout = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    assert os.environ.get("MVP046_EXPECTED_HEAD", checkout) == checkout

    stack.verify_database()
    probe = prepare_smtp(config, compose, stack, prefix, temporary)

    def domain(operation, **extra):
        stack.verify_database()
        return json.loads(
            compose(
                "exec",
                "-T",
                "api",
                "python",
                "-c",
                Path("scripts/mvp046_fixture.py").read_text(),
                operation,
                input_text=json.dumps(
                    {
                        "login": "browser-admin",
                        "password": config["passwords"]["admin"],
                        **extra,
                    }
                ),
            )
        )

    config.update(domain("internal"))
    config["checksum"] = stack.sql(
        "SELECT checksum_sha256 FROM app.document_versions v JOIN app.documents d ON d.id=v.document_id WHERE d.code='SYNTHETIC035'"
    )
    process = subprocess.Popen(
        ["node", "frontend/tests/mvp046-e2e.mjs"],
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    timer = threading.Timer(600, process.kill)
    timer.start()
    assert process.stdin and process.stdout and process.stderr
    checkpoints = []
    baseline = None
    receipt_numbers = []
    outbox_identity = None

    def snapshot():
        rows = stack.sql(
            "SELECT jsonb_build_array("
            + ",".join(
                f"(SELECT jsonb_agg(to_jsonb(t) ORDER BY id) FROM app.{table} t)"
                for table in (
                    "orders",
                    "consents",
                    "order_items",
                    "order_status_history",
                )
            )
            + ")"
        )
        return hashlib.sha256(rows.encode()).digest()

    def counts():
        return [
            int(n)
            for n in stack.sql(
                "SELECT "
                + ",".join(
                    f"(SELECT count(*) FROM app.{table})"
                    for table in (
                        "orders",
                        "consents",
                        "order_items",
                        "email_outbox",
                        "order_status_history",
                    )
                )
            ).split("|")
        ]

    def reply(value):
        process.stdin.write(json.dumps(value) + "\n")
        process.stdin.flush()

    try:
        reply(config)
        proof = None
        for line in process.stdout:
            event = json.loads(line)
            action = event["action"]
            stack.verify_database()
            result = True
            if action == "checkpoint":
                case = event["case"]
                assert case in CASES and case not in checkpoints
                checkpoints.append(case)
                print(
                    "MVP046_CHECKPOINT "
                    + json.dumps({"case": case, "result": "PASS", "counts": counts()}),
                    flush=True,
                )
            elif action in ("price", "consent", "revoke_public", "revoke_internal"):
                result = domain(action)
                if action == "revoke_public":
                    assert (
                        int(
                            stack.sql(
                                "SELECT count(*) FROM app.chat_messages WHERE response::text LIKE '%точная цитата для проверки%'"
                            )
                        )
                        > 0
                    ), "Stored JSONB must still contain original citation"
                if action == "revoke_internal":
                    assert (
                        stack.sql(
                            "SELECT count(*) FROM app.catalog_item_models WHERE public_allowed"
                        )
                        == "0"
                    )
            elif action == "committed":
                number = int(event["number"])
                assert number not in receipt_numbers
                receipt_numbers.append(number)
                # Joined rows prove the graph is the same committed order as receipt.
                assert (
                    stack.sql(f"""SELECT count(*) FROM app.orders o
                    JOIN app.consents c ON c.id=o.consent_id
                    JOIN app.email_outbox e ON e.order_id=o.id
                    WHERE o.number={number} AND o.status='new' AND o.version=1
                    AND c.channel='request_form' AND c.revoked_at IS NULL
                    AND e.event_kind='manager_notification' AND e.status='pending'
                    AND e.attempts=0 AND o.pii_erased_at IS NULL
                    AND (SELECT count(*) FROM app.order_status_history h WHERE h.order_id=o.id AND h.to_status='new' AND h.from_status IS NULL AND h.changed_by IS NULL)=1""")
                    == "1"
                )
                commercial = len(receipt_numbers) in (1, 3)
                if commercial:
                    price = "123.45" if len(receipt_numbers) == 1 else "150.00"
                    assert (
                        stack.sql(f"""SELECT count(*) FROM app.order_items i JOIN app.orders o ON o.id=i.order_id
                        WHERE o.number={number} AND o.kind='commercial' AND o.source='site'
                        AND o.items_total IS NULL AND o.total_amount IS NULL AND o.known_subtotal=2*{price} AND o.currency='RUB'
                        AND ((i.sku_snapshot='DEMO-P-001' AND i.qty=2 AND i.unit_price_snapshot={price} AND i.kind_snapshot='product')
                        OR (i.sku_snapshot='DEMO-S-001' AND i.qty=3 AND i.unit_price_snapshot IS NULL AND i.price_mode_snapshot='on_request' AND i.kind_snapshot='service'))""")
                        == "2"
                    )
                else:
                    comment = {
                        2: "SYNTHETIC046 support desktop",
                        4: "SYNTHETIC046 support mobile",
                        5: "SYNTHETIC046 independent expiry",
                    }[len(receipt_numbers)]
                    assert (
                        stack.sql(f"""SELECT count(*) FROM app.orders o WHERE number={number}
                        AND kind='support' AND comment='  {comment}  '
                        AND NOT EXISTS (SELECT FROM app.order_items i WHERE i.order_id=o.id)""")
                        == "1"
                    )
                result = counts()
            elif action == "image_state":
                result = json.loads(
                    stack.sql(
                        "SELECT jsonb_agg(jsonb_build_object('status',status,'image_key',image_key) ORDER BY sku) FROM app.catalog_items WHERE sku IN ('DEMO-P-001','DEMO-S-001')"
                    )
                )
            elif action == "baseline":
                baseline = snapshot()
            elif action == "unchanged":
                assert baseline is not None and baseline == snapshot(), (
                    "Business graph changed"
                )
            elif action == "fail":
                baseline = snapshot()
                result = probe("fail")
                outbox_identity = stack.sql(
                    "SELECT jsonb_build_array(id,order_id,event_kind,created_at) FROM app.email_outbox"
                )
                assert baseline == snapshot()
            elif action == "retried":
                assert outbox_identity == stack.sql(
                    "SELECT jsonb_build_array(id,order_id,event_kind,created_at) FROM app.email_outbox"
                )
                assert baseline == snapshot()
                assert (
                    stack.sql(
                        "SELECT count(*) FROM app.email_outbox WHERE status='pending' AND attempts=0 AND last_error_code IS NULL"
                    )
                    == "1"
                )
                assert (
                    stack.sql(
                        "SELECT count(*) FROM app.audit_events a JOIN app.staff_users u ON u.id=a.actor_id WHERE a.action='notification.retry' AND a.result='success' AND u.login='browser-manager'"
                    )
                    == "1"
                )
            elif action == "send":
                result = probe("send")
                assert outbox_identity == stack.sql(
                    "SELECT jsonb_build_array(id,order_id,event_kind,created_at) FROM app.email_outbox"
                )
                assert baseline == snapshot()
            elif action == "status":
                number = int(event["number"])
                assert (
                    stack.sql(f"""SELECT count(*) FROM app.orders o JOIN app.order_status_history h ON h.order_id=o.id JOIN app.staff_users u ON u.id=h.changed_by
                    WHERE o.number={number} AND o.status='in_progress' AND o.version=2
                    AND h.from_status='new' AND h.to_status='in_progress' AND u.login='browser-manager'
                    AND h.at=o.updated_at AND (SELECT count(*) FROM app.order_status_history h2 WHERE h2.order_id=o.id)=2""")
                    == "1"
                )
            elif action in ("expire", "delete_chat", "recover_message_limit"):
                identifier = str(UUID(event["session_id"]))
                if action == "expire":
                    stack.sql(
                        f"WITH t AS (SELECT clock_timestamp() at) UPDATE app.chat_sessions SET created_at=at-interval '25 hours',last_activity_at=at-interval '25 hours',expires_at=at-interval '1 hour' FROM t WHERE id='{identifier}'"
                    )
                elif action == "recover_message_limit":
                    # Advance only the owned synthetic session's quota window;
                    # production rates/thresholds and Nginx configuration stay intact.
                    subject = hashlib.sha256(UUID(identifier).bytes).hexdigest()
                    # Hash format is asserted against the implementation below.
                    result = stack.sql(
                        f"UPDATE app.rate_limit_buckets SET started_at=started_at-interval '2 minutes',expires_at=expires_at-interval '2 minutes' WHERE scope='message' AND subject_hash='{subject}' RETURNING used"
                    )
                    assert result == "30"
                else:
                    number = int(event["number"])
                    assert (
                        stack.sql(
                            f"SELECT count(*) FROM app.orders WHERE number={number} AND chat_session_id='{identifier}'"
                        )
                        == "1"
                    )
                    before = stack.sql(
                        f"SELECT to_jsonb(o)-'chat_session_id' FROM app.orders o WHERE number={number}"
                    )
                    # Witnessed штатная cleanup service, scoped to this session.
                    domain("chat_cleanup", session_id=identifier)
                    assert (
                        stack.sql(
                            f"SELECT to_jsonb(o)-'chat_session_id' FROM app.orders o WHERE number={number}"
                        )
                        == before
                    )
                    assert (
                        stack.sql(
                            f"SELECT chat_session_id IS NULL FROM app.orders WHERE number={number}"
                        )
                        == "t"
                    )
            elif action == "done":
                proof = event["proof"]
                validate_result(checkpoints, counts(), receipt_numbers)
                assert (
                    stack.sql(
                        "SELECT count(*) FROM app.email_outbox WHERE status='sent'"
                    )
                    == "1"
                )
                assert (
                    stack.sql(
                        "SELECT count(*) FROM app.email_outbox WHERE status='pending'"
                    )
                    == "4"
                )
                proof.update(orders=5, items=4, history=7)
                break
            else:
                raise AssertionError("Unknown fixed browser action")
            reply(result)
        process.stdin.close()
        process.wait(timeout=20)
        if process.returncode or proof is None:
            # Child stderr contains closed stage IDs only. Do not print a raw
            # Playwright exception, DOM, transport body or SQL exception.
            raise RuntimeError("MVP046_BROWSER_FAILED " + process.stderr.read()[-300:])
        evidence = {
            "result": "PASS",
            "head_sha": subprocess.check_output(
                ["git", "rev-parse", "HEAD"], text=True
            ).strip(),
            "cases": checkpoints,
            "counts": counts(),
            "seconds": round(monotonic() - started, 2),
            "postgresql": stack.sql("SHOW server_version"),
            "target_staging": "BLOCKED",
        }
        proof["mvp046_evidence"] = evidence
        return subprocess.CompletedProcess(process.args, 0, json.dumps(proof), "")
    finally:
        timer.cancel()
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)


if __name__ == "__main__":
    try:
        main(mvp046=True)
    except Exception as error:
        import traceback

        if isinstance(error, RuntimeError) and str(error).startswith(
            "MVP046_BROWSER_FAILED"
        ):
            print(str(error), flush=True)
        if isinstance(error, subprocess.CalledProcessError):
            diagnostic = (error.stdout or "") + (error.stderr or "")
            for needle, code in [
                ("No space left on device", "ENOSPC"),
                ("CERTIFICATE_VERIFY_FAILED", "BUILD_TLS_CA"),
                ("Permission denied", "PERMISSION_DENIED"),
            ]:
                if needle.lower() in diagnostic.lower():
                    print("MVP046_INFRA_" + code, flush=True)
        frames = traceback.extract_tb(error.__traceback__)
        print(
            "MVP046_FAILURE_LOCATION "
            + type(error).__name__
            + " "
            + ",".join(f"{Path(f.filename).name}:{f.lineno}" for f in frames),
            flush=True,
        )
        # Never leak child command arguments (operator credentials are on stdin)
        # or captured container output on failure.
        raise SystemExit(
            "MVP046_FAILED: inspect safe checkpoint/stage output; LOCAL_SYNTHETIC_E2E is not PASS"
        ) from None
