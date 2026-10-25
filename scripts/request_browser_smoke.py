"""Focused MVP-013 acceptance using the existing disposable trusted HTTPS stack.

A bounded line protocol permits fixed synthetic database changes while Chromium
holds an open form. It never accepts arbitrary SQL or an external DB target.
No traces, screenshots, request bodies or raw secrets are written to artifacts.
"""

import json
import subprocess
import threading

from https_smoke import main


def run_browser(environment: dict, config: dict, compose):
    process = subprocess.Popen(
        ["node", "frontend/tests/request-form.mjs"],
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    # Hard bound prevents an assertion/protocol failure from orphaning the stack.
    timer = threading.Timer(180, process.kill)
    timer.start()
    assert process.stdin and process.stdout and process.stderr

    def sql(statement: str) -> str:
        return compose(
            "exec",
            "-T",
            "postgres",
            "psql",
            "-U",
            "akbrizcase_owner",
            "-d",
            "akbrizcase",
            "-v",
            "ON_ERROR_STOP=1",
            "-Atc",
            statement,
        ).strip()

    try:
        process.stdin.write(json.dumps(config) + "\n")
        process.stdin.flush()
        proof = None
        for line in process.stdout:
            event = json.loads(line)
            action = event.get("action")
            if action == "done":
                proof = event["proof"]
                break
            if action == "counts":
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
            elif action == "change_consent":
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
                sql(
                    "UPDATE app.catalog_items SET price=150.00, revision=revision+1 WHERE sku='DEMO-P-001'"
                )
                result = True
            elif action == "archive_service":
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
