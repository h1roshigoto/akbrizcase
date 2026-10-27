"""Real admin forms on the existing guarded HTTPS/PostgreSQL acceptance stack."""

import json
import os
import re
import subprocess
import threading

from https_smoke import main


def run_browser(environment, config, compose, stack):
    stack.verify_database()
    process = subprocess.Popen(
        ["node", "frontend/tests/catalog-admin.mjs"],
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    timer = threading.Timer(240, process.kill)
    timer.start()
    assert process.stdin and process.stdout and process.stderr
    baseline = None
    checks = 0
    proof = None
    try:
        process.stdin.write(json.dumps(config) + "\n")
        process.stdin.flush()
        for line in process.stdout:
            event = json.loads(line)
            if event["action"] == "done":
                proof = event["proof"]
                break
            assert event["action"] in {"snapshot", "unchanged"}
            stack.verify_database()
            # Compare complete rows privately; never print PII or verification data.
            rows = stack.sql(
                "SELECT jsonb_build_array("
                + ",".join(
                    f"(SELECT jsonb_agg(to_jsonb(t) ORDER BY id) FROM app.{table} t)"
                    for table in (
                        "orders",
                        "order_items",
                        "consents",
                        "order_status_history",
                        "email_outbox",
                    )
                )
                + ")"
            )
            if event["action"] == "snapshot":
                assert baseline is None
                assert stack.sql("SELECT count(*) FROM app.orders") == "1"
                assert stack.sql("SELECT count(*) FROM app.order_items") == "2"
                baseline = rows
            else:
                assert baseline is not None and baseline == rows, "Order rows changed"
                checks += 1
            process.stdin.write('{"ok":true}\n')
            process.stdin.flush()
        process.stdin.close()
        process.wait(timeout=15)
        error = process.stderr.read()
        if process.returncode:
            return subprocess.CompletedProcess(
                process.args, process.returncode, "", error
            )
        assert proof is not None and checks >= 3
        # Every catalog mutation has an admin-authored, allowlisted audit event.
        assert (
            stack.sql("""
          SELECT count(*) FROM app.audit_events a
          JOIN app.staff_users u ON u.id=a.actor_id
          WHERE a.entity_type IN ('catalog_item','equipment_model')
            AND a.result='success' AND u.login='browser-admin'
            AND a.metadata - ARRAY['status','revision'] = '{}'::jsonb
        """)
            == "14"
        )
        assert (
            stack.sql("SELECT revision FROM app.catalog_items WHERE sku='MVP016-P'")
            == "6"
        )
        assert (
            stack.sql("SELECT status FROM app.catalog_items WHERE sku='MVP016-S'")
            == "archived"
        )
        checkout = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True
        ).strip()
        head = os.environ.get("MVP016_HEAD_SHA", checkout)
        assert re.fullmatch(r"[0-9a-f]{40}", head)
        proof["catalog_acceptance"] = {
            "head_sha": head,
            "checkout_sha": checkout,
            "postgresql_version": stack.sql("SHOW server_version"),
            "immutable_order_rows": True,
            "checks": checks,
            "admin_audit_events": 14,
            "media_acceptance": "JPEG/PNG SQLAdmin upload, anonymous React list/detail/selection, replace/remove, failed replacement",
            "stale_form_status": 409,
            "manager_get_post_status": 403,
            "xss_executed": False,
        }
        return subprocess.CompletedProcess(process.args, 0, json.dumps(proof), "")
    finally:
        timer.cancel()
        if process.poll() is None:
            process.kill()
        process.wait(timeout=15)


if __name__ == "__main__":
    main(catalog_admin=True)
