"""Fail-closed SMTP configuration and payload-free manager notifications."""

import os
import re
import smtplib
import socket
import ssl
import stat
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.policy import SMTP
from pathlib import Path
from threading import Event, Timer

from app.core.csrf import https_origin
from app.modules.notify.outbox import Claim

TIMEOUT = 20
_DOMAIN = (
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+"
)
_ADDRESS = re.compile(r"[A-Za-z0-9_+.-]{1,64}@(" + _DOMAIN + r")\Z")
_HOST = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*\Z"
)


class SMTPConfigurationError(ValueError):
    def __init__(self) -> None:
        super().__init__("SMTP configuration unavailable")


class SendFailure(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _safe(value: str) -> bool:
    return bool(value) and value.isascii() and all(32 < ord(c) < 127 for c in value)


def _mailbox(value: str) -> bool:
    local = value.partition("@")[0]
    return (
        len(value) <= 254
        and _ADDRESS.fullmatch(value) is not None
        and not local.startswith(".")
        and not local.endswith(".")
        and ".." not in local
    )


@dataclass(frozen=True)
class SMTPSettings:
    host: str
    port: int
    mode: str
    sender: str
    recipient: str
    admin_origin: str
    trusted_origins: frozenset[str]
    username: str | None = field(default=None, repr=False)
    password: str | None = field(default=None, repr=False)
    synthetic: bool = False

    def __post_init__(self) -> None:
        if (
            not _safe(self.host)
            or len(self.host) > 253
            or not _HOST.fullmatch(self.host)
            or not 1 <= self.port <= 65535
            or not _mailbox(self.sender)
            or not _mailbox(self.recipient)
            or not _safe(self.admin_origin)
            or not https_origin(self.admin_origin)
            or self.admin_origin not in self.trusted_origins
            or not all(https_origin(o) for o in self.trusted_origins)
            or self.mode not in {"tls", "starttls", "test"}
            or (
                self.mode == "test"
                and (
                    not self.synthetic
                    or self.host not in {"smtp-test", "127.0.0.1"}
                    or self.username is not None
                    or self.password is not None
                )
            )
            or (self.mode != "test" and self.synthetic)
            or ((self.username is None) != (self.password is None))
            or (self.username is not None and not _safe(self.username))
            or (
                self.password is not None
                and (
                    not self.password
                    or len(self.password) > 4096
                    or any(c in self.password for c in "\r\n\x00")
                )
            )
        ):
            raise SMTPConfigurationError()

    @classmethod
    def from_environment(cls) -> "SMTPSettings | None":
        names = (
            "SMTP_HOST",
            "SMTP_PORT",
            "SMTP_MODE",
            "SMTP_FROM",
            "SMTP_TO",
            "SMTP_ADMIN_ORIGIN",
            "SMTP_USERNAME",
            "SMTP_PASSWORD_FILE",
            "SMTP_TEST_DOUBLE",
        )
        if not any(os.environ.get(name) for name in names):
            return None
        try:
            password = None
            if filename := os.environ.get("SMTP_PASSWORD_FILE"):
                path = Path(filename)
                details = path.stat()
                if not stat.S_ISREG(details.st_mode) or details.st_mode & 0o022:
                    raise ValueError
                with path.open() as stream:
                    password = stream.read(4098).removesuffix("\n")
            synthetic = os.environ.get("SMTP_TEST_DOUBLE") == "1"
            if synthetic and os.environ.get("APP_ENV") != "test":
                raise ValueError
            return cls(
                host=os.environ["SMTP_HOST"],
                port=int(os.environ["SMTP_PORT"]),
                mode=os.environ["SMTP_MODE"],
                sender=os.environ["SMTP_FROM"],
                recipient=os.environ["SMTP_TO"],
                admin_origin=os.environ["SMTP_ADMIN_ORIGIN"],
                trusted_origins=frozenset(
                    os.environ["STAFF_TRUSTED_ORIGINS"].split(",")
                ),
                username=os.environ.get("SMTP_USERNAME"),
                password=password,
                synthetic=synthetic,
            )
        except (KeyError, OSError, ValueError, UnicodeError):
            raise SMTPConfigurationError() from None


def notification(settings: SMTPSettings, claim: Claim) -> EmailMessage:
    if claim.number <= 0:
        raise ValueError("Invalid notification number")
    message = EmailMessage(policy=SMTP)
    message["From"] = settings.sender
    message["To"] = settings.recipient
    message["Subject"] = f"Order #{claim.number}"
    message["Message-ID"] = f"<outbox.{claim.id}@{settings.sender.rsplit('@', 1)[1]}>"
    message.set_content(
        f"Order #{claim.number}\n"
        f"{settings.admin_origin}/admin/orders/{claim.order_id}\n"
    )
    return message


def send_notification(settings: SMTPSettings, claim: Claim) -> None:
    message = notification(settings, claim)
    try:
        context = ssl.create_default_context()
        client: smtplib.SMTP
        if settings.mode == "tls":
            client = smtplib.SMTP_SSL(timeout=TIMEOUT, context=context)
        else:
            client = smtplib.SMTP(timeout=TIMEOUT)
        expired = Event()

        def abort() -> None:
            expired.set()
            connection = client.sock
            if connection is not None:
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

        # Bound the entire exchange as well as individual blocking socket calls.
        watchdog = Timer(TIMEOUT, abort)
        watchdog.daemon = True
        watchdog.start()
        try:
            client.connect(settings.host, settings.port)
            if expired.is_set():
                raise SendFailure("SMTP_TIMEOUT")
            client.ehlo()
            if settings.mode == "starttls":
                client.starttls(context=context)
                client.ehlo()
            if settings.username is not None and settings.password is not None:
                client.login(settings.username, settings.password)
            refused = client.send_message(
                message, from_addr=settings.sender, to_addrs=[settings.recipient]
            )
            if refused:
                raise SendFailure("SMTP_REJECTED")
        except (OSError, smtplib.SMTPException):
            if expired.is_set():
                raise SendFailure("SMTP_TIMEOUT") from None
            raise
        finally:
            # Closing never sends QUIT after a failed DATA read. A second SMTP
            # operation could mask the original timeout or fail after acceptance.
            watchdog.cancel()
            client.close()
    except TimeoutError:
        raise SendFailure("SMTP_TIMEOUT") from None
    except ssl.SSLError:
        raise SendFailure("SMTP_TLS") from None
    except smtplib.SMTPAuthenticationError:
        raise SendFailure("SMTP_AUTH") from None
    except smtplib.SMTPServerDisconnected as error:
        # smtplib wraps socket read timeouts; inspect types, never server text.
        code = (
            "SMTP_TIMEOUT"
            if isinstance(error.__context__, TimeoutError)
            else "SMTP_UNAVAILABLE"
        )
        raise SendFailure(code) from None
    except (
        smtplib.SMTPRecipientsRefused,
        smtplib.SMTPSenderRefused,
        smtplib.SMTPDataError,
    ):
        raise SendFailure("SMTP_REJECTED") from None
    except (OSError, smtplib.SMTPException):
        raise SendFailure("SMTP_UNAVAILABLE") from None
