"""Isolated HTTP source. No DB credentials, host ports, proxy trust or raw output.

Run only with the controller-created private config and read-only local CA.
All HTTP travels to the real Nginx through a distinct container network address.
"""

import copy
import http.client
import json
import secrets
import socket
import ssl
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID, uuid4


def receipt(data):
    assert set(data) == {
        "number",
        "status",
        "known_subtotal",
        "items_total",
        "delivery_status",
        "delivery_cost",
        "total_amount",
        "currency",
    }
    assert type(data["number"]) is int and data["number"] > 0
    assert data["status"] == "new" and data["currency"] == "RUB"
    assert data["known_subtotal"] == "0.00" and data["items_total"] is None
    assert (
        data["delivery_status"] == "not_required"
        and data["delivery_cost"] == "0.00"
        and data["total_amount"] is None
    )


def created_session(data):
    assert set(data) == {
        "session_id",
        "session_token",
        "state_version",
        "expires_at",
        "reply",
    }
    UUID(data["session_id"])
    assert type(data["state_version"]) is int and data["state_version"] == 0
    import re

    assert type(data["session_token"]) is str and re.fullmatch(
        "[0-9a-f]{64}", data["session_token"]
    )


class LocalTLS(http.client.HTTPSConnection):
    def connect(self):
        raw = socket.create_connection(("nginx", 8443), self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname="localhost")
        except BaseException:
            raw.close()
            raise


class Client:
    def __init__(self, config):
        self.config = config
        self.samples = []
        self.count = 0
        self.orders = 0
        self.sessions = []
        self.phase = "preflight"
        self.current = config["client_ids"][0]
        self.context = ssl.create_default_context(cafile="/load/ca.crt")
        self.deadline = time.monotonic() + config["total_timeout"]
        self.lock = threading.Lock()
        self.last_retry_after = None

    def request(
        self,
        operation,
        method,
        path,
        body=None,
        headers=None,
        expected=200,
        validate=None,
    ):
        with self.lock:
            self.count += 1
            if self.count > 240 or time.monotonic() >= self.deadline:
                raise RuntimeError("REQUEST_BUDGET_EXHAUSTED")
        payload = json.dumps(body).encode() if body is not None else None
        start = time.monotonic_ns()
        code, ok, timeout, checked, data, response_headers = (
            0,
            False,
            False,
            False,
            None,
            {},
        )
        connection = LocalTLS("localhost", 8443, timeout=8, context=self.context)
        try:
            connection.request(
                method,
                path,
                payload,
                {"Content-Type": "application/json", **(headers or {})},
            )
            response = connection.getresponse()
            code = response.status
            response_headers = dict(response.getheaders())
            raw = response.read(1024 * 1024 + 1)
            assert len(raw) <= 1024 * 1024
            data = json.loads(raw)
            if path.startswith("/api/"):
                UUID(response.getheader("X-Request-ID"))
                assert response.getheader("Cache-Control") == "no-store"
            assert code in ([expected] if type(expected) is int else expected)
            if code >= 400:
                assert set(data) == {"error", "request_id"}
                UUID(data["request_id"])
                assert set(data["error"]) == {"code", "message", "details"}
                if code == 429:
                    assert data["error"]["code"] == "RATE_LIMITED"
                    assert 1 <= int(response.getheader("Retry-After")) <= 3600
                    self.last_retry_after = int(response.getheader("Retry-After"))
            if validate:
                validate(data)
            checked = True
            ok = 200 <= code < 300
        except TimeoutError:
            timeout = True
            code = 0
        except Exception:
            # Response/exception text can contain contacts, tokens, SourceRef or
            # transport internals. Retain only closed status and validation flag.
            pass
        finally:
            connection.close()
            self.samples.append(
                {
                    "phase": self.phase,
                    "operation": operation,
                    "status": code,
                    "ok": ok,
                    "timeout": timeout,
                    "start_ns": start,
                    "end_ns": time.monotonic_ns(),
                    "client": self.current,
                }
            )
        if self.phase != "measurement" and code in (
            [expected] if type(expected) is int else expected
        ):
            # Expected negative checks are successful functional assertions,
            # although they can never become successful business samples.
            assert data is not None and checked
            if code >= 400:
                assert "error" in data
                if code == 429:
                    assert (
                        int(
                            next(
                                v
                                for k, v in response_headers.items()
                                if k.lower() == "retry-after"
                            )
                        )
                        >= 1
                    )
            elif not ok:
                raise RuntimeError("HTTP_CONTRACT_INVALID")
        elif self.phase != "measurement":
            raise RuntimeError("HTTP_CONTRACT_INVALID")
        return code, data, response_headers

    def message(self, session, input_value, *, expected=200, version=None, token=True):
        expected_version = session["version"] if version is None else version

        def check(data):
            if "error" in data:
                return
            assert set(data) == {"message_id", "state_version", "reply"}
            UUID(data["message_id"])
            assert (
                type(data["state_version"]) is int
                and data["state_version"] == expected_version + 1
            )
            if input_value["type"] == "button":
                assert data["reply"]["resolution"] == "results"
                assert data["reply"]["text"] == "Синтетический утверждённый результат"
            if input_value["type"] == "start_diagnosis":
                assert data["reply"]["resolution"] == "question"
                assert any(
                    b["input"] == {"type": "button", "option_id": "a"}
                    for b in data["reply"]["buttons"]
                )

        status, data, _ = self.request(
            "button",
            "POST",
            "/api/v1/chat/messages",
            {
                "session_id": session["id"],
                "request_id": str(uuid4()),
                "expected_version": expected_version,
                "input": input_value,
            },
            {"X-Chat-Token": session["token"]} if token else {},
            expected=expected,
            validate=check if expected == 200 or isinstance(expected, list) else None,
        )
        if status == 200 and data and "state_version" in data:
            session["version"] = data["state_version"]
        return status

    def start(self, session):
        return self.message(
            session,
            {
                "type": "start_diagnosis",
                "model_id": self.config["model_id"],
                "tree_key": "SYNTHETIC035",
            },
        )

    def new_session(self):
        _, data, _ = self.request(
            "button",
            "POST",
            "/api/v1/chat/sessions",
            {"mode": "diagnose"},
            expected=201,
            validate=created_session,
        )
        return {"id": data["session_id"], "token": data["session_token"], "version": 0}

    def order(self, *, body=None, headers=None, expected=201):
        if headers is None:
            headers = {
                "Idempotency-Key": str(uuid4()),
                "X-Request-Token": secrets.token_hex(32),
            }
        status, data, response_headers = self.request(
            "order",
            "POST",
            "/api/v1/orders",
            body or self.body,
            headers,
            expected=expected,
            validate=receipt
            if expected in (200, 201) or isinstance(expected, list)
            else None,
        )
        if status == 201:
            with self.lock:
                self.orders += 1
                assert self.orders <= 5
        return status, data, response_headers

    def searches(self):
        def knowledge(data):
            assert set(data) == {"items", "total", "limit", "offset"}
            assert data["total"] == 1 and len(data["items"]) == 1
            assert (
                data["items"][0]["source"]["document_id"] == self.config["document_id"]
            )
            assert (
                data["items"][0]["excerpt"]
                == "Синтетическийквантопылесос: точная цитата для проверки."
            )

        def catalog(data):
            assert set(data) == {"items", "total", "limit", "offset"}
            assert data["total"] == 50 and data["items"]

        self.request(
            "knowledge",
            "GET",
            "/api/v1/knowledge/search?q=%D0%A1%D0%B8%D0%BD%D1%82%D0%B5%D1%82%D0%B8%D1%87%D0%B5%D1%81%D0%BA%D0%B8%D0%B9%D0%BA%D0%B2%D0%B0%D0%BD%D1%82%D0%BE%D0%BF%D1%8B%D0%BB%D0%B5%D1%81%D0%BE%D1%81",
            validate=knowledge,
        )
        self.request(
            "catalog",
            "GET",
            "/api/v1/catalog/items?q=SYNTHETIC%20LIMIT",
            validate=catalog,
        )

    def prepare(self):
        self.request("catalog", "GET", "/healthz")
        self.request("catalog", "GET", "/readyz")
        _, policy, _ = self.request("order", "GET", "/api/v1/privacy-policy")
        assert policy["synthetic"] and policy["collection_enabled"]
        _, consent, _ = self.request("order", "GET", "/api/v1/consent-text")
        self.body = {
            "kind": "commercial",
            "source": "site",
            "items": [
                {
                    "item_id": self.config["item_id"],
                    "qty": 1,
                    "expected_revision": self.config["revision"],
                }
            ],
            "contact": {"name": "SYNTHETIC048 ONLY", "email": "load@example.invalid"},
            "consent": {"version": consent["version"], "accepted": True},
            "confirmed": True,
        }
        self.phase = "prepare"
        for identifier in self.config["client_ids"]:
            self.current = identifier
            session = self.new_session()
            self.start(session)
            self.sessions.append(session)
        self.phase = "warmup"
        self.current = self.config["client_ids"][0]
        self.message(self.sessions[0], {"type": "button", "option_id": "a"})
        self.start(self.sessions[0])
        self.searches()
        self.order()

    def measure(self, start_ns):
        self.phase = "measurement"
        delay = (start_ns - time.monotonic_ns()) / 1e9
        assert 0 < delay <= 5
        time.sleep(delay)
        self.order()
        self.order()
        spacing = self.config["measurement_seconds"] / len(self.sessions)
        for index, session in enumerate(self.sessions):
            due = start_ns / 1e9 + index * spacing
            time.sleep(max(0, due - time.monotonic()))
            self.current = self.config["client_ids"][index]
            self.message(session, {"type": "button", "option_id": "a"})
            self.searches()
        assert (
            time.monotonic_ns()
            <= start_ns + (self.config["measurement_seconds"] + 8) * 10**9
        )

    def functional(self):
        self.phase = "functional"
        if self.config["source"] != 0:
            return
        session = self.sessions[0]
        self.current = self.config["client_ids"][0]
        self.message(
            session, {"type": "button", "option_id": "a"}, version=0, expected=409
        )
        self.message(
            session,
            {"type": "select_mode", "mode": "knowledge"},
            token=False,
            expected=404,
        )
        invalid = copy.deepcopy(self.body)
        invalid["consent"]["version"] = "SYNTHETIC048-MISSING"
        self.order(body=invalid, expected=409)
        invalid = copy.deepcopy(self.body)
        invalid["items"][0]["expected_revision"] += 1
        self.order(body=invalid, expected=409)
        headers = {
            "Idempotency-Key": str(uuid4()),
            "X-Request-Token": secrets.token_hex(32),
        }
        barrier = threading.Barrier(2)

        def replay(_):
            barrier.wait(timeout=5)
            return self.order(headers=headers, expected=[200, 201])[:2]

        with ThreadPoolExecutor(max_workers=2) as executor:
            receipts = list(executor.map(replay, range(2)))
        assert sorted(code for code, _ in receipts) == [200, 201]
        assert receipts[0][1] == receipts[1][1]
        self.order()
        spoof = {
            "X-Forwarded-For": "198.51.100.42",
            "X-Akbriz-Client-IP": "198.51.100.43",
            "Idempotency-Key": str(uuid4()),
            "X-Request-Token": secrets.token_hex(32),
        }
        self.order(headers=spoof, expected=429)
        # Exhaust only real HTTP successes; no quota SQL, time shifts or resets.
        for _ in range(10 - len(self.sessions)):
            self.new_session()
        self.request(
            "button",
            "POST",
            "/api/v1/chat/sessions",
            {"mode": "diagnose"},
            spoof,
            expected=429,
        )
        # Replay still works at an exhausted new-order quota.
        self.order(headers=headers, expected=200)
        # Real session-quota expiry, with no synthetic SQL/time adjustment.
        for _ in range(35):
            status = self.message(
                session,
                {"type": "select_mode", "mode": "knowledge"},
                expected=[200, 429],
            )
            if status == 429:
                assert 1 <= self.last_retry_after <= 60
                time.sleep(self.last_retry_after + 0.1)
                self.message(session, {"type": "select_mode", "mode": "knowledge"})
                break
        else:
            raise RuntimeError("MESSAGE_QUOTA_NOT_OBSERVED")
        # Nginx burst saturation is observed separately and followed by real
        # elapsed-time recovery. This source has at most 240 requests overall.
        seen_429 = False
        while self.count < 200:
            code, _, response_headers = self.request(
                "catalog",
                "GET",
                "/api/v1/catalog/items?q=SYNTHETIC%20LIMIT",
                expected=[200, 429],
            )
            if code == 429:
                seen_429 = True
                retry = int(
                    next(
                        v
                        for k, v in response_headers.items()
                        if k.lower() == "retry-after"
                    )
                )
                assert retry <= 2  # edge quota, not the hour-long business quota
                time.sleep(retry + 0.1)
                self.request(
                    "catalog", "GET", "/api/v1/catalog/items?q=SYNTHETIC%20LIMIT"
                )
                break
        assert seen_429


def emit(value):
    print(json.dumps(value, allow_nan=False), flush=True)


def main():
    config = json.loads(sys.stdin.readline())
    assert set(config) == {
        "source",
        "client_ids",
        "measurement_seconds",
        "total_timeout",
        "model_id",
        "document_id",
        "item_id",
        "revision",
    }
    assert 1 <= len(config["client_ids"]) <= 8 and 0 <= config["source"] < 30
    global active_client
    client = active_client = Client(config)
    client.prepare()
    emit({"stage": "ready", "source": config["source"]})
    command = json.loads(sys.stdin.readline())
    assert set(command) == {"start_ns"}
    client.measure(command["start_ns"])
    # Finish measurement before functional replay (which has its own concurrency).
    emit({"stage": "measured", "source": config["source"]})
    assert json.loads(sys.stdin.readline()) == {"functional": True}
    client.functional()
    emit(
        {
            "stage": "done",
            "source": config["source"],
            "orders": client.orders,
            "samples": client.samples,
            "requests": client.count,
        }
    )


if __name__ == "__main__":
    try:
        main()
    except BaseException as error:
        import traceback

        # Never print exceptions, config, bodies, token-bearing argv or traceback.
        client = globals().get("active_client")
        emit(
            {
                "stage": "failed",
                "lines": [
                    f.lineno
                    for f in traceback.extract_tb(error.__traceback__)
                    if f.filename == __file__
                ],
                "samples": client.samples if client else [],
            }
        )
        raise SystemExit(1) from None
