"""Exercise a disposable Compose project using synthetic data only.

The caller builds images first. Volumes are preserved after the unique test
project stops; the regular demo project and its data are never touched.
"""

import json
import os
import re
import secrets
import socket
import subprocess
import tempfile
import uuid
from concurrent.futures import ThreadPoolExecutor
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener

from synthetic_stack import SyntheticStack, isolated_environment, synthetic_secrets


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="akbrizcase-smoke-") as temporary:
        run(temporary)


def run(temporary: str, *, worker_acceptance: bool = False) -> None:
    environment = isolated_environment(os.environ)
    synthetic_secrets(temporary, environment)
    environment["APP_ENV"] = "demo"  # Only this disposable, owner-marked stack.
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        environment["HTTP_PORT"] = str(listener.getsockname()[1])
    project = "akbrizcase-smoke-" + uuid.uuid4().hex[:10]
    prefix = [
        "docker",
        "compose",
        "--env-file",
        "/dev/null",
        "-f",
        "docker-compose.yml",
        "--project-name",
        project,
    ]

    def compose(*args: str, input_text: str | None = None) -> str:
        result = subprocess.run(
            [*prefix, *args],
            input=input_text,
            text=True,
            capture_output=True,
            env=environment,
            check=True,
            timeout=180,
        )
        return result.stdout.strip()

    def inspect(service: str) -> dict:
        container = compose("ps", "--all", "--quiet", service)
        result = subprocess.run(
            ["docker", "inspect", container],
            capture_output=True,
            text=True,
            check=True,
            timeout=15,
        )
        return json.loads(result.stdout)[0]

    def sql(query: str) -> str:
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
            query,
        )

    opener = build_opener(ProxyHandler({}))

    def http(path: str) -> tuple[int, str]:
        try:
            with opener.open(
                f"http://127.0.0.1:{environment['HTTP_PORT']}{path}",
                timeout=5,
            ) as response:
                return response.status, response.read().decode()
        except HTTPError as response:
            return response.code, response.read().decode()

    stack = SyntheticStack(project, compose, environment)
    prefix += ["-f", str(stack.write_override(temporary))]
    try:
        stack.claim()
        compose("config", "--quiet")
        compose("up", "--no-build", "--detach", "--wait", "--wait-timeout", "120")
        stack.verify_volumes()
        print("PASS: Compose started", flush=True)
        # Startup must never silently create or migrate the schema.
        assert http("/readyz") == (503, '{"status":"unavailable"}')
        compose("run", "--rm", "migrate")
        compose("run", "--rm", "migrate")
        assert http("/readyz") == (200, '{"status":"ok"}')
        assert (
            sql("SELECT version_num FROM app.alembic_version")
            == "0011_mvp039_http_security"
        )
        assert sql(
            "SELECT string_agg(tablename, ',' ORDER BY tablename) FROM pg_tables "
            "WHERE schemaname = 'app'"
        ) == (
            "alembic_version,audit_events,catalog_item_models,catalog_items,"
            "chat_messages,chat_sessions,cleanup_cursors,"
            "consent_publications,consent_texts,consents,deletion_journal,"
            "diag_tree_items,diag_tree_sources,diag_trees,"
            "document_sections,document_versions,documents,email_outbox,equipment_models,"
            "order_items,order_status_history,orders,"
            "staff_sessions,staff_users"
        )
        print(
            "PASS: explicit/repeat migrations and readiness through Nginx", flush=True
        )
        # The ordinary stack has no marker. Even an accidental APP_ENV=demo
        # cannot seed it. These invocations must fail without writing any rows.
        for arguments in (
            ("run", "--rm", "seed", "--mode", "demo"),
            ("run", "--rm", "-e", "APP_ENV=demo", "seed", "--mode", "demo"),
        ):
            try:
                compose(*arguments)
            except subprocess.CalledProcessError:
                pass
            else:
                raise AssertionError("Unmarked/production database accepted seed")
        assert sql("SELECT count(*) FROM app.catalog_items") == "0"
        assert sql("SELECT count(*) FROM app.consent_texts") == "0"
        try:
            compose(
                "run",
                "--rm",
                "-e",
                "APP_ENV=demo",
                "--entrypoint",
                "python",
                "seed",
                "-m",
                "app.modules.orders.seed",
                "--mode",
                "demo",
            )
        except subprocess.CalledProcessError:
            pass
        else:
            raise AssertionError("Unmarked database accepted synthetic consent")
        # Only this unique, disposable smoke project is labelled by its owner.
        sql("COMMENT ON DATABASE akbrizcase IS 'akbrizcase:synthetic-seed:demo'")
        stack.verify_database()
        for _ in range(2):
            compose("run", "--rm", "-e", "APP_ENV=demo", "seed", "--mode", "demo")
        assert sql("SELECT count(*) FROM app.catalog_items") == "4"
        assert sql("SELECT count(*) FROM app.equipment_models") == "1"
        for _ in range(2):
            compose(
                "run",
                "--rm",
                "-e",
                "APP_ENV=demo",
                "--entrypoint",
                "python",
                "seed",
                "-m",
                "app.modules.orders.seed",
                "--mode",
                "demo",
            )
        assert sql("SELECT count(*) FROM app.consent_texts") == "1"
        assert (
            sql(
                "SELECT purpose FROM app.consent_texts "
                "WHERE version = 'SYNTHETIC-DEMO-ONLY-v1'"
            )
            == "SYNTHETIC DEMO/TEST ONLY — NOT LEGALLY APPROVED"
        )
        try:
            compose("run", "--rm", "-e", "APP_ENV=production", "seed", "--mode", "demo")
        except subprocess.CalledProcessError:
            pass
        else:
            raise AssertionError("Production environment accepted seed")
        print("PASS: seed guards and idempotent synthetic demo catalog", flush=True)
        # Actual HTTP submission and committed SQL evidence in this isolated DB.
        assert http("/api/v1/consent-text")[0] == 200
        selections = json.loads(sql(
            "SELECT json_agg(json_build_object('item_id', id, 'qty', 2, "
            "'expected_revision', revision) ORDER BY id) FROM app.catalog_items "
            "WHERE sku IN ('DEMO-P-001', 'DEMO-P-002', 'DEMO-S-001')"
        ))
        body = {
            "kind": "commercial", "items": selections,
            "contact": {"name": "Synthetic Smoke", "email": "smoke@example.invalid"},
            "comment": "Synthetic request for isolated acceptance only",
            "source": "site",
            "consent": {"version": "SYNTHETIC-DEMO-ONLY-v1", "accepted": True},
            "confirmed": True,
        }
        submission_token = secrets.token_hex(32)
        original_order_body = body
        submit = Request(
            f"http://127.0.0.1:{environment['HTTP_PORT']}/api/v1/orders",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "Idempotency-Key": str(uuid.uuid4()),
                     "X-Request-Token": submission_token},
            method="POST",
        )
        with opener.open(submit, timeout=10) as response:
            assert response.status == 201
            assert response.headers["Cache-Control"] == "no-store"
            receipt = json.load(response)
        assert receipt == {
            "number": receipt["number"], "status": "new", "known_subtotal": "382.68",
            "items_total": None, "delivery_status": "pending_quote",
            "delivery_cost": None, "total_amount": None, "currency": "RUB",
        }
        assert isinstance(receipt["number"], int) and receipt["number"] > 0
        evidence = json.loads(sql(
            "SELECT json_build_object('orders', (SELECT count(*) FROM app.orders), "
            "'consents', (SELECT count(*) FROM app.consents), "
            "'items', (SELECT count(*) FROM app.order_items), "
            "'history', (SELECT count(*) FROM app.order_status_history), "
            "'pending_outbox', (SELECT count(*) FROM app.email_outbox WHERE "
            "status='pending' AND event_kind='manager_notification'))"
        ))
        assert evidence == {"orders": 1, "consents": 1, "items": 3, "history": 1, "pending_outbox": 1}
        assert sql(
            "SELECT count(*) FROM app.orders o JOIN app.consents c ON c.id=o.consent_id "
            "JOIN app.order_status_history h ON h.order_id=o.id "
            "JOIN app.email_outbox e ON e.order_id=o.id "
            "WHERE o.status='new' AND o.version=1 AND c.text_version="
            "'SYNTHETIC-DEMO-ONLY-v1' AND h.from_status IS NULL "
            "AND h.to_status='new' AND h.changed_by IS NULL "
            "AND e.status='pending'"
        ) == "1"
        # MVP-010 acceptance through Nginx, including real concurrent POSTs.
        def post_order(payload: dict, headers: dict) -> tuple[int, dict]:
            request = Request(submit.full_url, data=json.dumps(payload).encode(),
                              headers=headers, method="POST")
            try:
                result = opener.open(request, timeout=15)
            except HTTPError as error:
                result = error
            with result:
                assert result.headers["Cache-Control"] == "no-store"
                return result.status, json.load(result)

        # urllib adds Content-Length to the first Request; each new body needs
        # its own length, including shorter conflict/negative test bodies.
        original_headers = {name: value for name, value in submit.header_items()
                            if name.lower() != "content-length"}
        status, replay = post_order(body, original_headers)
        assert status == 200 and replay == receipt
        changed = body | {"comment": "Changed synthetic request"}
        status, conflict = post_order(changed, original_headers)
        assert status == 409 and conflict["error"]["code"] == "IDEMPOTENCY_CONFLICT"
        wrong_headers = original_headers | {"X-request-token": "0" * 64}
        for payload in (body, changed):
            status, hidden = post_order(payload, wrong_headers)
            assert status == 404 and hidden["error"] == {
                "code": "NOT_FOUND", "message": "Object not found", "details": {},
            }
        status, invalid = post_order(body, original_headers | {"X-request-token": "invalid"})
        assert status == 422 and invalid["error"]["code"] == "VALIDATION_ERROR"
        concurrent_headers = original_headers | {"Idempotency-key": str(uuid.uuid4())}
        with ThreadPoolExecutor(max_workers=4) as pool:
            concurrent = list(pool.map(lambda _: post_order(body, concurrent_headers), range(4)))
        assert sorted(status for status, _ in concurrent) == [200, 200, 200, 201]
        assert all(value == concurrent[0][1] for _, value in concurrent)
        concurrent_counts = json.loads(sql(
            "SELECT json_build_array((SELECT count(*) FROM app.orders), "
            "(SELECT count(*) FROM app.consents), (SELECT count(*) FROM app.order_items), "
            "(SELECT count(*) FROM app.order_status_history), (SELECT count(*) FROM app.email_outbox))"
        ))
        assert concurrent_counts == [2, 2, 6, 2, 2]
        print("PASS: HTTP replay=200, conflict=409, hidden=404, transport=422; "
              "parallel POST=[201,200,200,200], SQL counts=" + json.dumps(concurrent_counts), flush=True)
        logs = compose("logs", "--no-color", "api", "nginx", "postgres")
        audit_and_outbox = sql(
            "SELECT coalesce(json_agg(row_to_json(a))::text,'[]') FROM app.audit_events a"
        ) + sql(
            "SELECT coalesce(json_agg(row_to_json(e))::text,'[]') FROM app.email_outbox e"
        )
        for private in ("Synthetic Smoke", "smoke@example.invalid", body["comment"], submission_token):
            assert private not in logs + audit_and_outbox + json.dumps(receipt)
        print("PASS: HTTP POST 201 sanitized receipt " + json.dumps(receipt), flush=True)
        print("PASS: committed SQL write set " + json.dumps(evidence), flush=True)
        # Public API acceptance through the real Nginx -> API -> PostgreSQL path.
        # All additional rows belong only to this marked disposable smoke DB.
        sql("""
            INSERT INTO app.catalog_items
                (id, sku, kind, name, description, unit, price_mode, price,
                 tax_note, status, image_key)
            VALUES
                ('00000000-0000-4000-8000-000000000007', 'SMOKE-S-007',
                 'service', 'Синтетическая услуга', 'Только тест', 'услуга',
                 'on_request', NULL, 'ТЕСТ', 'published', '/private/internal.png'),
                ('00000000-0000-4000-8000-000000000008', 'SMOKE-DRAFT',
                 'product', 'Скрытая позиция', 'Только тест', 'шт.',
                 'on_request', NULL, 'ТЕСТ', 'draft', NULL),
                ('00000000-0000-4000-8000-000000000009', 'SMOKE-ARCHIVED',
                 'product', 'Архивная позиция', 'Только тест', 'шт.',
                 'on_request', NULL, 'ТЕСТ', 'archived', NULL);
            INSERT INTO app.equipment_models (id, code, name, description, status)
            VALUES
                ('00000000-0000-4000-8000-000000000008', 'SMOKE-DRAFT',
                 'Скрытая модель', 'Только тест', 'draft'),
                ('00000000-0000-4000-8000-000000000009', 'SMOKE-ARCHIVED',
                 'Архивная модель', 'Только тест', 'archived');
        """)

        def api_json(path: str, expected_status: int = 200) -> dict:
            status, body = http(path)
            assert status == expected_status, (path, status, body)
            value = json.loads(body)
            if expected_status != 200:
                assert set(value) == {"error", "request_id"}
                assert str(uuid.UUID(value["request_id"])) == value["request_id"]
                assert value["error"]["details"] == {}
            return value

        catalog = api_json("/api/v1/catalog/items")
        assert (catalog["total"], catalog["limit"], catalog["offset"]) == (5, 20, 0)
        rows = {row["sku"]: row for row in catalog["items"]}
        assert rows["DEMO-P-001"]["price"] == "123.45"
        assert rows["DEMO-P-003"]["price"] == "0.00"
        assert rows["SMOKE-S-007"]["price"] is None
        assert rows["SMOKE-S-007"]["price_mode"] == "on_request"
        models = api_json("/api/v1/equipment-models")
        assert models["total"] == 1 and len(models["items"]) == 1
        model_id = models["items"][0]["id"]
        for row in rows.values():
            path = f"/api/v1/catalog/items/{row['id']}"
            detail = api_json(path)
            assert {key: detail[key] for key in row} == row
            assert detail["image_url"] is None and detail["applicability"] is None
            assert "image_key" not in detail and "/private/" not in json.dumps(detail)
            assert api_json(path + "?model_id=" + model_id)["applicability"] == {
                "model_id": model_id,
                "status": "unknown",
                "sources": [],
            }
        for kind, total in (("product", 3), ("service", 2)):
            result = api_json(f"/api/v1/catalog/items?kind={kind}&limit=1&offset=1")
            assert result["total"] == total and len(result["items"]) == 1
            assert result["items"][0]["kind"] == kind
        for query in ("фильтр", "DEMO-P-001", "P-001"):
            result = api_json("/api/v1/catalog/items?" + urlencode({"q": query}))
            assert result["total"] == 1
            assert result["items"][0]["sku"] == "DEMO-P-001"
        assert api_json("/api/v1/catalog/items?q=missing")["items"] == []
        assert api_json("/api/v1/catalog/items?limit=100&offset=10000")["items"] == []
        for identifier in (
            "00000000-0000-4000-8000-000000000008",
            "00000000-0000-4000-8000-000000000009",
            "00000000-0000-4000-8000-000000000099",
        ):
            api_json("/api/v1/catalog/items/" + identifier, 404)
            api_json(
                f"/api/v1/catalog/items/{rows['DEMO-P-001']['id']}?model_id={identifier}",
                404,
            )
        for identifier in (model_id, "00000000-0000-4000-8000-000000000008"):
            result = api_json("/api/v1/catalog/items?model_id=" + identifier)
            assert result["items"] == [] and result["total"] == 0
        for query in ("limit=101", "offset=10001", "kind=invalid", "model_id=invalid"):
            api_json("/api/v1/catalog/items?" + query, 422)
        api_json("/api/v1/catalog/items?" + urlencode({"q": "я" * 1001}), 422)
        api_json("/api/v1/catalog/items/invalid", 422)
        api_json(f"/api/v1/catalog/items/{rows['DEMO-P-001']['id']}/image/key.png", 404)
        print(
            "PASS: public catalog/search/DTOs/hidden IDs/applicability through Nginx",
            flush=True,
        )
        status, body = http("/healthz")
        assert status == 200 and json.loads(body) == {"status": "ok"}
        status, page = http("/")
        assert status == 200 and '<div id="root"></div>' in page
        assets = re.findall(r'(?:src|href)="(/assets/[^\"]+)"', page)
        assert assets
        bundles = [http(asset) for asset in assets]
        assert all(status == 200 for status, _ in bundles)
        assert any("B2B-портал" in body for _, body in bundles)
        status, body = http("/api/v1/not-implemented")
        assert status == 404 and json.loads(body)["error"]["code"] == "NOT_FOUND"
        print("PASS: Nginx health, UI/assets and API proxy", flush=True)

        api, worker, db, nginx = (
            inspect(s) for s in ("api", "worker", "postgres", "nginx")
        )
        assert api["Image"] == worker["Image"]
        for backend in (api, worker):
            mounted_secrets = {
                m["Destination"]
                for m in backend["Mounts"]
                if m["Destination"].startswith("/run/secrets/")
            }
            expected = {"/run/secrets/runtime_password"}
            if backend is api:
                expected.add("/run/secrets/staff_csrf_secret")
                expected.add("/run/secrets/order_hmac_secret")
                expected.add("/run/secrets/ip_hmac_secret")
            assert mounted_secrets == expected
            assert all(
                "POSTGRES_PASSWORD" not in value for value in backend["Config"]["Env"]
            )
        print(
            "PASS: runtime credentials only; CSRF/HMAC keys restricted to API",
            flush=True,
        )
        for service in (api, worker, db):
            assert not service["HostConfig"]["PortBindings"]
        bindings = nginx["HostConfig"]["PortBindings"]["8080/tcp"]
        assert all(item["HostIp"] == "127.0.0.1" for item in bindings)
        assert not (
            nginx["NetworkSettings"]["Networks"].keys()
            & db["NetworkSettings"]["Networks"].keys()
        )
        for network in (project + "_web", project + "_data"):
            result = subprocess.run(
                ["docker", "network", "inspect", network],
                capture_output=True,
                text=True,
                check=True,
                timeout=15,
            )
            assert json.loads(result.stdout)[0]["Internal"] is True
        # Authenticate via TCP from the DB container, then check TCP reachability
        # from both backend services without adding a database driver to MVP-001.
        compose(
            "exec",
            "-T",
            "postgres",
            "sh",
            "-ec",
            'PGPASSWORD="$(cat /run/secrets/postgres_password)" '
            "psql -h postgres -U akbrizcase_owner -d akbrizcase "
            '-v ON_ERROR_STOP=1 -Atc "SELECT 1" >/dev/null',
        )
        try:
            compose(
                "exec",
                "-T",
                "postgres",
                "sh",
                "-ec",
                "PGPASSWORD=invalid-smoke-password psql -h postgres "
                "-U akbrizcase_owner -d akbrizcase -Atc 'SELECT 1'",
            )
        except subprocess.CalledProcessError:
            pass
        else:
            raise AssertionError("PostgreSQL accepted an invalid TCP password")
        for service in ("api", "worker"):
            compose(
                "exec",
                "-T",
                service,
                "python",
                "-c",
                "import socket; socket.create_connection(('postgres', 5432), 3).close()",
            )
        assert sql("SHOW server_version").startswith("16.")
        print(
            "PASS: internal PostgreSQL 16/authentication; no DB/API/worker published ports",
            flush=True,
        )

        compose("stop", "postgres")
        assert http("/healthz")[0] == 200
        assert http("/readyz") == (503, '{"status":"unavailable"}')
        for path in (
            "/api/v1/catalog/items",
            "/api/v1/equipment-models",
            f"/api/v1/catalog/items/{rows['DEMO-P-001']['id']}",
        ):
            assert api_json(path, 503)["error"]["code"] == "SERVICE_UNAVAILABLE"
        # Complete the start transition before Compose's health waiter observes
        # the container; some versions otherwise reuse the stopped exited(0).
        compose("start", "postgres")
        compose("up", "--no-build", "--detach", "--wait", "postgres")
        assert http("/readyz")[0] == 200
        assert api_json("/api/v1/catalog/items")["total"] == 5
        print("PASS: DB outage preserves liveness and fails readiness", flush=True)

        marker = uuid.uuid4().hex
        compose(
            "exec",
            "-T",
            "api",
            "python",
            "-c",
            "from pathlib import Path; p=Path('/var/lib/akbrizcase/files'); "
            "assert p.stat().st_mode & 0o777 == 0o700; "
            f"(p/'smoke-private.txt').write_text('{marker}')",
        )
        assert (
            compose(
                "exec",
                "-T",
                "worker",
                "python",
                "-c",
                "from pathlib import Path; print(Path('/var/lib/akbrizcase/files/smoke-private.txt').read_text())",
            )
            == marker
        )
        private_volume = next(
            m["Name"]
            for m in api["Mounts"]
            if m["Destination"] == "/var/lib/akbrizcase/files"
        )
        assert all(m.get("Name") != private_volume for m in nginx["Mounts"])
        status, body = http("/files/smoke-private.txt")
        assert status == 404 and marker not in body
        print(
            "PASS: private persistent file volume, shared by API/worker only",
            flush=True,
        )

        sql("CREATE TABLE mvp055_smoke (marker text NOT NULL)")
        sql(f"INSERT INTO mvp055_smoke VALUES ('{marker}')")
        compose("stop", "worker")
        assert inspect("worker")["State"]["ExitCode"] == 0
        assert "Worker stopped gracefully" in compose("logs", "--no-color", "worker")
        compose("stop")
        for service in ("api", "worker", "postgres", "nginx"):
            state = inspect(service)["State"]
            # Uvicorn re-raises SIGTERM after completing graceful ASGI shutdown.
            accepted_codes = (0, 143) if service == "api" else (0,)
            assert not state["Running"] and state["ExitCode"] in accepted_codes, (
                service,
                state,
            )
        assert "Application shutdown complete." in compose("logs", "--no-color", "api")
        compose("down")
        compose("up", "--no-build", "--detach", "--wait", "--wait-timeout", "120")
        stack.verify_database()
        assert sql("SELECT marker FROM mvp055_smoke") == marker
        assert (
            compose(
                "exec",
                "-T",
                "worker",
                "python",
                "-c",
                "from pathlib import Path; print(Path('/var/lib/akbrizcase/files/smoke-private.txt').read_text())",
            )
            == marker
        )
        assert http("/healthz")[0] == 200
        assert http("/readyz")[0] == 200
        print("PASS: graceful worker/stack stop; DB/files survive down/up", flush=True)
        if worker_acceptance:
            from worker_smoke import exercise_worker

            exercise_worker(
                temporary, prefix, compose, sql, inspect, stack,
                lambda: post_order(original_order_body, original_headers), receipt,
                ("Synthetic Smoke", "smoke@example.invalid",
                 "Synthetic request for isolated acceptance only", submission_token),
            )
    finally:
        stack.cleanup()


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as error:
        raise SystemExit(error.stderr.strip() or str(error)) from error
