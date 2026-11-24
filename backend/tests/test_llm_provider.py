"""Unit, negative, cross-process budget, and secret-canary tests for GigaChat."""

import json
import multiprocessing
import os
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from app.modules.llm import (
    FileBudgetLedger,
    GigaChatClient,
    GroundedCatalogItem,
    GroundedFragment,
    GroundedRequest,
    LLMCallMetrics,
    LLMConfigurationError,
    LLMError,
    LLMSettings,
    parse_grounded_output,
)


def _child_reserve(root_str: str, budget: int, rpm: int, queue: object) -> None:
    ledger = FileBudgetLedger(
        Path(root_str), daily_token_budget=budget, max_requests_per_minute=rpm
    )
    try:
        ledger.reserve(60)
        queue.put("OK")  # type: ignore[attr-defined]
    except LLMError as exc:
        queue.put(exc.code)  # type: ignore[attr-defined]


@contextmanager
def _local_server(
    handler_cls: type[BaseHTTPRequestHandler],
) -> Iterator[tuple[str, Any]]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}", server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_config_kill_switch_and_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("LLM_ENABLED", raising=False)
    assert LLMSettings.from_environment() is None

    monkeypatch.setenv("LLM_ENABLED", "false")
    assert LLMSettings.from_environment() is None

    monkeypatch.setenv("LLM_ENABLED", "true")
    monkeypatch.delenv("GIGACHAT_AUTH_KEY_FILE", raising=False)
    with pytest.raises(LLMConfigurationError):
        LLMSettings.from_environment()

    key_file = tmp_path / "gigachat.key"
    key_file.write_text("synthetic-auth-key", encoding="utf-8")
    monkeypatch.setenv("GIGACHAT_AUTH_KEY_FILE", str(key_file))
    monkeypatch.setenv("GIGACHAT_OAUTH_URL", "http://example.invalid/oauth")
    monkeypatch.delenv("LLM_SYNTHETIC", raising=False)
    with pytest.raises(LLMConfigurationError):
        LLMSettings.from_environment()

    bad_link = tmp_path / "link.key"
    os.symlink(key_file, bad_link)
    monkeypatch.setenv("GIGACHAT_AUTH_KEY_FILE", str(bad_link))
    monkeypatch.setenv(
        "GIGACHAT_OAUTH_URL", "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
    )
    with pytest.raises(LLMConfigurationError):
        LLMSettings.from_environment()

    monkeypatch.setenv("GIGACHAT_AUTH_KEY_FILE", str(key_file))
    loaded = LLMSettings.from_environment()
    assert loaded is not None
    assert "synthetic-auth-key" not in repr(loaded)


def test_parse_grounded_output_positive_and_negative() -> None:
    sec_id = uuid4()
    item_id = uuid4()
    fenced = (
        "```json\n"
        + json.dumps(
            {
                "answer": "Проверьте питание датчика.",
                "cited_section_ids": [str(sec_id), str(sec_id)],
                "cited_item_ids": [str(item_id)],
                "needs_handoff": False,
            }
        )
        + "\n```"
    )
    parsed = parse_grounded_output(fenced)
    assert parsed.answer == "Проверьте питание датчика."
    assert parsed.cited_section_ids == [sec_id]
    assert parsed.cited_item_ids == [item_id]

    for bad in (
        "",
        "not json",
        json.dumps({"answer": "   "}),
        json.dumps({"answer": "ok", "extra": 1}),
        "x" * 40000,
    ):
        with pytest.raises(LLMError) as exc:
            parse_grounded_output(bad)
        assert exc.value.code == "LLM_MALFORMED_OUTPUT"


def test_client_oauth_cache_list_models_and_grounded_completion() -> None:
    sec_id = uuid4()
    item_id = uuid4()
    state: dict[str, Any] = {
        "oauth_calls": 0,
        "rquids": [],
        "auth_headers": [],
    }

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            return

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(length)
            if self.path == "/oauth":
                state["oauth_calls"] += 1
                state["rquids"].append(self.headers.get("RqUID"))
                state["auth_headers"].append(self.headers.get("Authorization"))
                body = json.dumps(
                    {
                        "access_token": "tok-1",
                        "expires_at": int((time.time() + 1800) * 1000),
                    }
                ).encode()
            elif self.path == "/v1/chat/completions":
                body = json.dumps(
                    {
                        "choices": [
                            {
                                "message": {
                                    "content": json.dumps(
                                        {
                                            "answer": "Датчик стоит 4500 ₽.",
                                            "cited_section_ids": [str(sec_id)],
                                            "cited_item_ids": [str(item_id)],
                                            "needs_handoff": False,
                                        }
                                    )
                                }
                            }
                        ],
                        "usage": {
                            "prompt_tokens": 50,
                            "completion_tokens": 25,
                            "total_tokens": 75,
                        },
                    }
                ).encode()
            else:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path == "/v1/models":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(
                    json.dumps(
                        {"data": [{"id": "GigaChat-2-Pro"}, {"id": "GigaChat-2"}]}
                    ).encode()
                )
            else:
                self.send_response(404)
                self.end_headers()

    with _local_server(Handler) as (base, _):
        metrics: list[LLMCallMetrics] = []
        settings = LLMSettings(
            auth_key="secret-basic-key",
            oauth_url=f"{base}/oauth",
            api_base_url=f"{base}/v1",
            synthetic=True,
        )
        client = GigaChatClient(settings, on_metrics=metrics.append)
        models = client.list_models()
        assert models == ["GigaChat-2", "GigaChat-2-Pro"]

        req = GroundedRequest(
            question="Сколько стоит датчик?",
            fragments=[
                GroundedFragment(
                    section_id=sec_id,
                    title="Датчик",
                    text="Датчик передающий ЯИУШ.432228.001",
                )
            ],
            items=[
                GroundedCatalogItem(
                    item_id=item_id,
                    sku="ЯИУШ.432228.001",
                    name="Датчик передающий",
                    price_label="4500.00 RUB",
                )
            ],
        )
        answer, usage = client.generate_grounded(req)
        assert answer.answer == "Датчик стоит 4500 ₽."
        assert answer.cited_section_ids == [sec_id]
        assert answer.cited_item_ids == [item_id]
        assert usage.total_tokens == 75
        assert state["oauth_calls"] == 1
        assert state["auth_headers"] == ["Basic secret-basic-key"]
        assert state["rquids"][0]
        assert len(metrics) == 1
        assert metrics[0].outcome_code == "OK" and metrics[0].usage.total_tokens == 75


def test_client_retries_401_429_503_and_rejects_failures() -> None:
    sec_id = uuid4()
    counters: dict[str, Any] = {"oauth": 0, "comp": 0, "mode": "401_then_ok"}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            return

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(length)
            if self.path == "/oauth":
                counters["oauth"] += 1
                self.send_response(200)
                self.end_headers()
                self.wfile.write(
                    json.dumps(
                        {
                            "access_token": f"tok-{counters['oauth']}",
                            "expires_at": int((time.time() + 1800) * 1000),
                        }
                    ).encode()
                )
                return

            counters["comp"] += 1
            mode = counters["mode"]
            if mode == "401_then_ok":
                if counters["comp"] == 1:
                    self.send_response(401)
                    self.end_headers()
                    return
                self._send_ok()
            elif mode == "429_then_ok":
                if counters["comp"] == 1:
                    self.send_response(429)
                    self.send_header("Retry-After", "0.05")
                    self.end_headers()
                    return
                self._send_ok()
            elif mode == "always_401":
                self.send_response(401)
                self.end_headers()
            elif mode == "always_429":
                self.send_response(429)
                self.send_header("Retry-After", "0.05")
                self.end_headers()
            elif mode == "always_502":
                self.send_response(502)
                self.end_headers()
                self.wfile.write(b"CANARY-UPSTREAM-ERROR")
            elif mode == "not_found":
                self.send_response(404)
                self.end_headers()
            elif mode == "slow":
                time.sleep(0.5)
                self._send_ok()
            elif mode == "bad_json":
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"choices":[{"message":{"content":"bad"}}]}')
            elif mode == "oversized":
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"x" * (257 * 1024))

        def _send_ok(self) -> None:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(
                json.dumps(
                    {
                        "choices": [
                            {
                                "message": {
                                    "content": json.dumps(
                                        {
                                            "answer": "Готово",
                                            "cited_section_ids": [str(sec_id)],
                                            "cited_item_ids": [],
                                            "needs_handoff": False,
                                        }
                                    )
                                }
                            }
                        ],
                        "usage": {"total_tokens": 10},
                    }
                ).encode()
            )

    with _local_server(Handler) as (base, _):
        settings = LLMSettings(
            auth_key="canary-secret-key",
            oauth_url=f"{base}/oauth",
            api_base_url=f"{base}/v1",
            connect_timeout_s=0.1,
            read_timeout_s=0.1,
            max_retries=1,
            synthetic=True,
        )
        client = GigaChatClient(settings)
        req = GroundedRequest(
            question="canary-question-pii",
            fragments=[GroundedFragment(section_id=sec_id, title="T", text="Body")],
        )

        # 401 refresh then success
        ans, _ = client.generate_grounded(req)
        assert ans.answer == "Готово"
        assert counters["oauth"] == 2

        # 429 retry then success
        counters["comp"] = 0
        counters["mode"] = "429_then_ok"
        ans, _ = client.generate_grounded(req)
        assert ans.answer == "Готово"
        assert counters["comp"] == 2

        for mode, expected_code in (
            ("always_401", "LLM_AUTH_FAILED"),
            ("always_429", "LLM_RATE_LIMITED"),
            ("always_502", "LLM_UPSTREAM_ERROR"),
            ("not_found", "LLM_MODEL_UNAVAILABLE"),
            ("slow", "LLM_TIMEOUT"),
            ("bad_json", "LLM_MALFORMED_OUTPUT"),
            ("oversized", "LLM_MALFORMED_OUTPUT"),
        ):
            counters["comp"] = 0
            counters["mode"] = mode
            with pytest.raises(LLMError) as exc:
                client.generate_grounded(req)
            assert exc.value.code == expected_code
            msg = str(exc.value) + repr(client)
            assert "canary-secret-key" not in msg
            assert "CANARY-UPSTREAM-ERROR" not in msg
            assert "canary-question-pii" not in msg


def test_file_budget_ledger_enforces_across_processes_and_restarts(
    tmp_path: Path,
) -> None:
    root = tmp_path / "files"
    root.mkdir(mode=0o700)

    # 1. Cross-process daily budget enforcement (100 limit, 60 each -> 1 OK, 1 denied)
    ctx = multiprocessing.get_context("spawn")
    q: Any = ctx.Queue()
    p1 = ctx.Process(target=_child_reserve, args=(str(root), 100, 10, q))
    p2 = ctx.Process(target=_child_reserve, args=(str(root), 100, 10, q))
    p1.start()
    p2.start()
    p1.join(timeout=15)
    p2.join(timeout=15)
    assert p1.exitcode == 0 and p2.exitcode == 0
    outcomes = sorted([q.get(timeout=5), q.get(timeout=5)])
    assert outcomes == ["LLM_BUDGET_EXCEEDED", "OK"]

    # 2. Restart survival + RPM limit
    ledger_restarted = FileBudgetLedger(
        root, daily_token_budget=1000, max_requests_per_minute=2
    )
    ledger_restarted.reserve(10)
    with pytest.raises(LLMError) as rpm_exc:
        ledger_restarted.reserve(10)
    assert rpm_exc.value.code == "LLM_RATE_LIMITED"

    # 3. Symlink/permission rejection
    (root / "llm").chmod(0o755)
    with pytest.raises(LLMError) as perm_exc:
        ledger_restarted.reserve(10)
    assert perm_exc.value.code == "LLM_STORAGE_ERROR"
