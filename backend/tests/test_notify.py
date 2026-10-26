"""Transport configuration, MIME allowlist, TLS and safe failure mapping."""

import os
import smtplib
import ssl
from dataclasses import replace
from email import policy
from email.parser import BytesParser
from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from app.modules.notify.outbox import Claim
from app.modules.notify.smtp import (
    SendFailure,
    SMTPConfigurationError,
    SMTPSettings,
    notification,
    send_notification,
)


def settings() -> SMTPSettings:
    return SMTPSettings(
        "smtp.example.invalid",
        465,
        "tls",
        "notify@example.invalid",
        "manager@example.invalid",
        "https://staff.example.invalid",
        frozenset({"https://staff.example.invalid"}),
    )


def claim() -> Claim:
    return Claim(uuid4(), uuid4(), 73, uuid4(), 1)


@pytest.mark.parametrize(
    "field,value",
    [
        ("sender", "a@example.invalid\r\nBcc: other@example.invalid"),
        ("recipient", "a@example.invalid,b@example.invalid"),
        ("sender", "Display <a@example.invalid>"),
        ("recipient", "a@localhost"),
        ("recipient", ".a@example.invalid"),
        ("sender", "a..b@example.invalid"),
        ("sender", "a.@example.invalid"),
        ("host", "."),
        ("host", "-smtp.example.invalid"),
        ("host", "smtp.invalid\nsecret"),
        ("admin_origin", "http://staff.example.invalid"),
        ("admin_origin", "https://evil.invalid"),
        ("admin_origin", "https://staff.example.invalid/path"),
        ("admin_origin", "https://a@staff.example.invalid"),
        ("admin_origin", "https://staff.example.invalid?token=bad"),
        ("port", 0),
        ("port", 65536),
        ("mode", "plain"),
        ("mode", "test"),
        ("username", "user"),
        ("password", "secret"),
    ],
)
def test_invalid_settings(field: str, value: object) -> None:
    with pytest.raises(
        SMTPConfigurationError, match="^SMTP configuration unavailable$"
    ):
        replace(settings(), **{field: value})  # type: ignore[arg-type]


def test_disabled_partial_and_secret_file(tmp_path: Path) -> None:
    with patch.dict(os.environ, {}, clear=True):
        assert SMTPSettings.from_environment() is None
    with patch.dict(os.environ, {"SMTP_HOST": "smtp.example.invalid"}, clear=True):
        with pytest.raises(SMTPConfigurationError):
            SMTPSettings.from_environment()
    path = tmp_path / "secret"
    path.write_text("synthetic-auth-secret\n")
    path.chmod(0o400)
    environment = {
        "SMTP_HOST": "smtp.example.invalid",
        "SMTP_PORT": "465",
        "SMTP_MODE": "tls",
        "SMTP_FROM": "notify@example.invalid",
        "SMTP_TO": "manager@example.invalid",
        "SMTP_ADMIN_ORIGIN": "https://staff.example.invalid",
        "STAFF_TRUSTED_ORIGINS": "https://staff.example.invalid",
        "SMTP_USERNAME": "synthetic",
        "SMTP_PASSWORD_FILE": str(path),
    }
    with patch.dict(os.environ, environment, clear=True):
        configured = SMTPSettings.from_environment()
        assert configured is not None
        assert configured.password == "synthetic-auth-secret"
        assert "synthetic-auth-secret" not in repr(configured)
        path.chmod(0o666)
        with pytest.raises(SMTPConfigurationError):
            SMTPSettings.from_environment()
        path.chmod(0o400)
        path.chmod(0o600)
        path.write_text("bad\r\nheader")
        with pytest.raises(SMTPConfigurationError):
            SMTPSettings.from_environment()


def test_exact_mime_and_stable_message_id() -> None:
    item = claim()
    message = notification(settings(), item)
    parsed = BytesParser(policy=policy.default).parsebytes(message.as_bytes())
    assert set(parsed.keys()) == {
        "From",
        "To",
        "Subject",
        "Message-ID",
        "Content-Type",
        "Content-Transfer-Encoding",
        "MIME-Version",
    }
    assert str(parsed["From"]) == "notify@example.invalid"
    assert str(parsed["To"]) == "manager@example.invalid"
    assert str(parsed["Subject"]) == "Order #73"
    assert parsed.get_content() == (
        f"Order #73\r\nhttps://staff.example.invalid/admin/orders/{item.order_id}\r\n"
    )
    retry = replace(item, token=uuid4(), attempts=5)
    assert message["Message-ID"] == notification(settings(), retry)["Message-ID"]
    assert str(message["Message-ID"]) == f"<outbox.{item.id}@example.invalid>"


@pytest.mark.parametrize("mode", ["tls", "starttls"])
def test_tls_verification_and_timeout(mode: str) -> None:
    with (
        patch("app.modules.notify.smtp.smtplib.SMTP_SSL") as implicit,
        patch("app.modules.notify.smtp.smtplib.SMTP") as explicit,
    ):
        factory = implicit if mode == "tls" else explicit
        client = factory.return_value
        client.send_message.return_value = {}
        send_notification(replace(settings(), mode=mode), claim())
        assert factory.call_args.kwargs["timeout"] == 20
        context = (
            factory.call_args.kwargs["context"]
            if mode == "tls"
            else client.starttls.call_args.kwargs["context"]
        )
        assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
        if mode == "starttls":
            assert client.ehlo.call_count == 2


@pytest.mark.parametrize(
    "error,code",
    [
        (TimeoutError("private"), "SMTP_TIMEOUT"),
        (ssl.SSLError("private"), "SMTP_TLS"),
        (smtplib.SMTPAuthenticationError(535, b"private"), "SMTP_AUTH"),
        (smtplib.SMTPDataError(451, b"private"), "SMTP_REJECTED"),
        (OSError("private"), "SMTP_UNAVAILABLE"),
    ],
)
def test_safe_failures(error: Exception, code: str) -> None:
    factory = MagicMock(side_effect=error)
    with patch("app.modules.notify.smtp.smtplib.SMTP_SSL", factory):
        with pytest.raises(SendFailure) as caught:
            send_notification(settings(), claim())
    assert str(caught.value) == code
    assert "private" not in str(caught.value)


def test_starttls_failure_never_sends_or_falls_back() -> None:
    with patch("app.modules.notify.smtp.smtplib.SMTP") as factory:
        client = factory.return_value
        client.starttls.side_effect = smtplib.SMTPNotSupportedError("private")
        with pytest.raises(SendFailure):
            send_notification(replace(settings(), mode="starttls"), claim())
        client.send_message.assert_not_called()
        client.login.assert_not_called()
