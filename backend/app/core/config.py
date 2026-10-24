"""Non-secret defaults; no external services or runtime secrets are required."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    app_name: str = "akbrizcase API"
    debug: bool = False


settings = Settings()
