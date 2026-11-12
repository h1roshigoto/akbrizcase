"""Explicit synthetic release gates; legacy demo/test behavior is unchanged."""

import hashlib
import json
import os
import re
import stat
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, StrictBool
from sqlalchemy import text
from sqlalchemy.orm import Session

if TYPE_CHECKING:
    from app.core.db import Database


class ReleaseModes(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    format: Literal[1]
    data: Literal["synthetic-only"]
    catalog: StrictBool
    orders: Literal[False]
    chat: Literal[False]
    knowledge: Literal[False]
    files: Literal[False]
    smtp: Literal["disabled"]
    clamav: Literal["disabled-files-blocked"]
    signature_update: Literal["disabled-files-blocked"]
    real_pii: Literal[False]


class FullReleaseModes(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    format: Literal[2]
    profile: Literal["full-synthetic-staging"]
    data: Literal["synthetic-only"]
    catalog: Literal[True]
    orders: Literal[True]
    chat: Literal[True]
    knowledge: Literal[True]
    files: Literal[True]
    smtp: Literal["isolated-sink"]
    clamav: Literal["required"]
    signature_update: Literal["operator-verified-offline"]
    real_pii: Literal[False]


def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def modes_fingerprint(value: dict[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def full_authority(modes: FullReleaseModes) -> None:
    # This read-only controller witness is mounted from the private claimed root.
    # The local builder/daemon/operator are trusted, as in MVP-041; this is not
    # an attestation against a malicious host administrator.
    if os.environ.get("APP_ENV") != "staging":
        raise ValueError
    path = Path(os.environ["RELEASE_AUTHORITY_FILE"])
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_IMODE(info.st_mode) != 0o444
        or info.st_uid == os.geteuid()
        or info.st_size > 4096
    ):
        raise ValueError
    authority = json.loads(path.read_bytes(), object_pairs_hook=unique)
    if not isinstance(authority, dict):
        raise ValueError
    if (
        set(authority) != {"format", "scope", "project", "profile", "modes"}
        or type(authority["format"]) is not int
        or authority["format"] != 1
        or authority["scope"] != "local-synthetic-rehearsal"
        or authority["profile"] != modes.profile
        or not isinstance(authority["project"], str)
        or re.fullmatch(r"akbriz-staging-[0-9a-f]{12}", authority["project"]) is None
        or authority["project"] != os.environ.get("RELEASE_PROJECT")
        or authority["modes"] != modes_fingerprint(modes.model_dump())
        or os.environ.get("DB_HOST") != "postgres"
        or os.environ.get("DB_NAME") != "akbrizcase"
    ):
        raise ValueError


def release_required() -> bool:
    return os.environ.get("APP_ENV") == "staging" or "RELEASE_CONFIG_FILE" in os.environ


def release_modes() -> ReleaseModes | FullReleaseModes | None:
    if not release_required():
        return None
    try:
        raw = Path(os.environ["RELEASE_CONFIG_FILE"]).read_bytes()
        if len(raw) > 4096:
            raise ValueError

        value = json.loads(raw, object_pairs_hook=unique)
        if not isinstance(value, dict):
            raise ValueError
        if type(value.get("format")) is not int:
            raise ValueError
        # Literal validation accepts 0/1 for booleans in Pydantic; reject those
        # explicitly, including real_pii, rather than treating JSON numbers as flags.
        for key in ("catalog", "orders", "chat", "knowledge", "files", "real_pii"):
            if type(value.get(key)) is not bool:
                raise ValueError
        if value["format"] == 2:
            full = FullReleaseModes.model_validate(value)
            full_authority(full)
            # SMTP parameters are constructed in code, never environment overrides.
            if any(k.startswith("SMTP_") and v for k, v in os.environ.items()):
                raise ValueError
            return full
        modes = ReleaseModes.model_validate(value)
        # An injected SMTP setting cannot silently enable a disabled transport.
        if any(key.startswith("SMTP_") and value for key, value in os.environ.items()):
            raise ValueError
        return modes
    except (KeyError, OSError, ValueError, UnicodeError):
        raise ValueError("RELEASE_CONFIG_INVALID") from None


def full_synthetic_database(session: Session) -> bool:
    if not isinstance(release_modes(), FullReleaseModes):
        return False
    # Reuse the owner-only marker understood by the existing SQL seed function.
    # APP_ENV remains staging. The marker alone cannot authorize this profile.
    return bool(
        session.scalar(
            text(
                "SELECT shobj_description(oid, 'pg_database') FROM pg_database "
                "WHERE datname = current_database()"
            )
        )
        == "akbrizcase:synthetic-seed:demo"
    )


def validate_session(session: Session) -> None:
    if isinstance(release_modes(), FullReleaseModes) and not full_synthetic_database(
        session
    ):
        raise ValueError("RELEASE_DATABASE_INVALID")


@lru_cache(maxsize=1)
def file_guard_database() -> "Database":
    # Cache only a bounded connection pool, never a marker or authorization
    # result. This adds at most one connection per process, including the worker.
    from app.core.db import Database, DatabaseSettings

    return Database(DatabaseSettings.from_environment(), release_guard=True)


def require_files() -> None:
    modes = release_modes()
    if modes is None:
        return
    if not isinstance(modes, FullReleaseModes):
        raise ValueError("RELEASE_FILES_DISABLED")
    # CLI/file entry points also verify the real DB marker, even without HTTP.
    with file_guard_database().unit_of_work():
        pass


def release_denied(path: str, method: str) -> bool:
    try:
        modes = release_modes()
    except ValueError:
        return path not in {"/healthz", "/readyz"}
    if modes is None:
        return False
    if isinstance(modes, FullReleaseModes):
        return False  # Existing domain ACL, staff RBAC, Origin/CSRF still apply.
    # Evaluate the decoded ASGI path, including normalized trailing slashes.
    path = path.rstrip("/")
    if path.startswith("/api/v1/"):
        resource = path[len("/api/v1/") :].split("/", 1)[0]
        enabled = {
            "catalog": modes.catalog,
            "equipment-models": modes.catalog,
            "orders": modes.orders,
            "chat": modes.chat,
            "knowledge": modes.knowledge,
            "documents": False,
        }
        if resource in enabled and not enabled[resource]:
            return True
        if "/images/" in path or "/image/" in path or path.endswith("/image"):
            return True
    # All staff mutations are closed in the initial reduced staging profile.
    # Login/logout retain their existing authentication, Origin and CSRF checks.
    if path.startswith(("/admin", "/api/v1/admin")):
        return method not in {"GET", "HEAD", "OPTIONS"} and not path.endswith(
            ("/auth/login", "/auth/logout")
        )
    return False
