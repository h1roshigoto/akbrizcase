"""Fail-closed server-side GigaChat configuration (kill-switch OFF by default)."""

import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

DEFAULT_OAUTH_URL = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
DEFAULT_API_BASE_URL = "https://gigachat.devices.sberbank.ru/api/v1"
DEFAULT_SCOPE = "GIGACHAT_API_PERS"
DEFAULT_MODEL = "GigaChat-2"


class LLMConfigurationError(ValueError):
    def __init__(self) -> None:
        super().__init__("LLM configuration unavailable")


def _valid_url(url: str, *, synthetic: bool) -> bool:
    if not url or len(url) > 512 or any(c.isspace() for c in url):
        return False
    parsed = urlparse(url)
    if parsed.scheme == "https":
        return bool(parsed.netloc)
    if synthetic and parsed.scheme == "http":
        return parsed.hostname == "127.0.0.1" and parsed.port is not None
    return False


def _read_secret_file(path_value: str) -> str:
    path = Path(path_value)
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise LLMConfigurationError()
        if info.st_size <= 0 or info.st_size > 4096:
            raise LLMConfigurationError()
        secret = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        raise LLMConfigurationError() from None
    if not secret or len(secret) > 4096 or any(c in secret for c in "\r\n\x00"):
        raise LLMConfigurationError()
    return secret


@dataclass(frozen=True)
class LLMSettings:
    auth_key: str = field(repr=False)
    scope: str = DEFAULT_SCOPE
    model: str = DEFAULT_MODEL
    oauth_url: str = DEFAULT_OAUTH_URL
    api_base_url: str = DEFAULT_API_BASE_URL
    ca_bundle_file: Path | None = None
    connect_timeout_s: float = 3.0
    read_timeout_s: float = 12.0
    max_retries: int = 1
    max_tokens: int = 700
    temperature: float = 0.2
    daily_token_budget: int = 200_000
    max_requests_per_minute: int = 30
    synthetic: bool = False

    def __post_init__(self) -> None:
        if (
            not self.auth_key
            or len(self.auth_key) > 4096
            or not self.auth_key.isascii()
            or any(c in self.auth_key for c in "\r\n\x00")
            or not self.scope
            or len(self.scope) > 128
            or not self.model
            or len(self.model) > 100
            or not _valid_url(self.oauth_url, synthetic=self.synthetic)
            or not _valid_url(self.api_base_url, synthetic=self.synthetic)
            or not 0.1 <= self.connect_timeout_s <= 15.0
            or not 0.1 <= self.read_timeout_s <= 60.0
            or not 0 <= self.max_retries <= 3
            or not 64 <= self.max_tokens <= 4096
            or not 0.0 <= self.temperature <= 1.0
            or not 1 <= self.daily_token_budget <= 10_000_000
            or not 1 <= self.max_requests_per_minute <= 600
        ):
            raise LLMConfigurationError()
        if self.ca_bundle_file is not None:
            try:
                info = self.ca_bundle_file.lstat()
                if not stat.S_ISREG(info.st_mode):
                    raise LLMConfigurationError()
            except OSError:
                raise LLMConfigurationError() from None

    @classmethod
    def from_environment(cls) -> "LLMSettings | None":
        enabled = os.environ.get("LLM_ENABLED", "").strip().lower()
        if enabled in {"", "0", "false", "no", "off"}:
            return None
        if enabled not in {"1", "true", "yes", "on"}:
            raise LLMConfigurationError()
        try:
            key_file = os.environ["GIGACHAT_AUTH_KEY_FILE"]
            auth_key = _read_secret_file(key_file)
            ca_env = os.environ.get("GIGACHAT_CA_BUNDLE_FILE", "").strip()
            ca_path = Path(ca_env) if ca_env else None
            synthetic = os.environ.get("LLM_SYNTHETIC", "").strip() == "1"
            return cls(
                auth_key=auth_key,
                scope=os.environ.get("GIGACHAT_SCOPE", DEFAULT_SCOPE).strip(),
                model=os.environ.get("GIGACHAT_MODEL", DEFAULT_MODEL).strip(),
                oauth_url=os.environ.get(
                    "GIGACHAT_OAUTH_URL", DEFAULT_OAUTH_URL
                ).strip(),
                api_base_url=os.environ.get(
                    "GIGACHAT_API_BASE_URL", DEFAULT_API_BASE_URL
                )
                .strip()
                .rstrip("/"),
                ca_bundle_file=ca_path,
                connect_timeout_s=float(
                    os.environ.get("GIGACHAT_CONNECT_TIMEOUT_S", "3.0")
                ),
                read_timeout_s=float(os.environ.get("GIGACHAT_READ_TIMEOUT_S", "12.0")),
                max_retries=int(os.environ.get("GIGACHAT_MAX_RETRIES", "1")),
                max_tokens=int(os.environ.get("GIGACHAT_MAX_TOKENS", "700")),
                temperature=float(os.environ.get("GIGACHAT_TEMPERATURE", "0.2")),
                daily_token_budget=int(
                    os.environ.get("GIGACHAT_DAILY_TOKEN_BUDGET", "200000")
                ),
                max_requests_per_minute=int(os.environ.get("GIGACHAT_MAX_RPM", "30")),
                synthetic=synthetic,
            )
        except LLMConfigurationError:
            raise
        except (KeyError, ValueError):
            raise LLMConfigurationError() from None
