"""Projection timing uses the existing isolated immutable-content chat suite."""

import json
import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_chat_messages import body as message_body
from test_chat_messages import message_db as message_db
from test_chat_messages import saved, scenario, start
from test_chat_sessions import create
from test_request_timing_pg import observed_db as observed_db
from test_request_timing_pg import phases

from app.core.db import Database
from app.main import app

pytestmark = pytest.mark.integration
__all__ = ["message_db", "observed_db"]


def test_chat_post_commit_projection_and_replay_timed_separately(
    message_db: Database,
    observed_db: Database,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    c, _, key = scenario(message_db, tmp_path)
    with (
        caplog.at_level(logging.INFO, logger="akbrizcase.runtime"),
        TestClient(app, client=("192.0.2.1", 50000)) as client,
    ):
        app.state.database = observed_db
        own = create(client)
        headers = {"X-Chat-Token": own["session_token"]}
        assert (
            client.post(
                "/api/v1/chat/messages", json=start(own, c, key), headers=headers
            ).status_code
            == 200
        )
        request = message_body(own, 1, type="button", option_id="a")
        caplog.clear()
        first = client.post("/api/v1/chat/messages", json=request, headers=headers)
        replay = client.post("/api/v1/chat/messages", json=request, headers=headers)
        denied = client.post(
            "/api/v1/chat/messages", json=request, headers={"X-Chat-Token": "f" * 64}
        )
    assert first.status_code == replay.status_code == 200
    assert first.json() == replay.json() and denied.status_code == 404
    events = phases(caplog)
    assert len(events) == 3
    for event in events[:2]:
        m = event["timings"]
        assert m["commit_calls"] == m["pool_checkouts"] == 2
        assert m["chat_write_ms"] > 0 and m["chat_projection_ms"] > 0
        assert 0 < m["projection_commit_ms"] < m["chat_projection_ms"]
        assert m["worker_ms"] >= m["chat_write_ms"] + m["chat_projection_ms"]
    assert events[2]["timings"]["rollback_calls"] == 1
    assert events[2]["timings"]["chat_projection_ms"] == 0
    row, messages = saved(message_db, own)
    assert row.state_version == len(messages) == 2
    assert own["session_token"] not in json.dumps(events)
