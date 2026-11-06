"""Explicit synthetic release gates; legacy demo/test behavior is unchanged."""

import json
import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, StrictBool


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


def release_required() -> bool:
    return os.environ.get("APP_ENV") == "staging" or "RELEASE_CONFIG_FILE" in os.environ


def release_modes() -> ReleaseModes | None:
    if not release_required():
        return None
    try:
        raw = Path(os.environ["RELEASE_CONFIG_FILE"]).read_bytes()
        if len(raw) > 4096:
            raise ValueError

        def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
            result: dict[str, object] = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError
                result[key] = value
            return result

        modes = ReleaseModes.model_validate(json.loads(raw, object_pairs_hook=unique))
        # An injected SMTP setting cannot silently enable a disabled transport.
        if any(key.startswith("SMTP_") and value for key, value in os.environ.items()):
            raise ValueError
        return modes
    except (KeyError, OSError, ValueError, UnicodeError):
        raise ValueError("RELEASE_CONFIG_INVALID") from None


def release_denied(path: str, method: str) -> bool:
    try:
        modes = release_modes()
    except ValueError:
        return path not in {"/healthz", "/readyz"}
    if modes is None:
        return False
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
