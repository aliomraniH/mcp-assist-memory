"""Application configuration — the single place environment/secrets are read.

Grep-gate: `os.environ` must not appear anywhere outside this module. Everything
else receives a `Settings` instance (or the cached `settings` singleton).
"""

from __future__ import annotations

import functools
from pathlib import Path
from typing import Any

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

MB = 1024 * 1024


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # -- secrets (Replit Secrets in production) ---------------------------------
    # database_url is optional so the SQLite-backed dev path and unit tests can
    # construct Settings without a Postgres; app.py asserts it is set for the
    # Postgres deployment.
    database_url: SecretStr | None = None
    mcp_auth_token: SecretStr = SecretStr("")

    # Declared now, unused until Phase 3 (embeddings/recall). Never read in Phase 0.
    voyage_api_key: SecretStr | None = None
    openai_api_key: SecretStr | None = None
    anthropic_api_key: SecretStr | None = None
    langsmith_api_key: SecretStr | None = None

    # -- limits -----------------------------------------------------------------
    # Defense-in-depth cap on a single artifact blob written to bytea (avoids OOM
    # on the Replit VM). Matches the per-upload limit by default.
    max_artifact_bytes: int = 25 * MB
    max_upload_mb: int = 25
    max_total_storage_mb: int = 500

    # -- SQLite dev/test backend only ------------------------------------------
    data_dir: Path = Path("./data")

    # -- server -----------------------------------------------------------------
    port: int = 8000
    log_level: str = "INFO"

    # -- derived ----------------------------------------------------------------
    @property
    def auth_token(self) -> str:
        return self.mcp_auth_token.get_secret_value()

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * MB

    @property
    def max_total_storage_bytes(self) -> int:
        return self.max_total_storage_mb * MB

    @property
    def max_zip_decompressed_bytes(self) -> int:
        return 4 * self.max_upload_bytes

    def database_url_str(self) -> str:
        if self.database_url is None:
            raise RuntimeError("DATABASE_URL is required for the Postgres backend")
        return self.database_url.get_secret_value()

    def as_log_safe(self) -> dict[str, Any]:
        """Startup-loggable view — never includes secret values."""
        return {
            "has_database_url": self.database_url is not None,
            "max_artifact_mb": self.max_artifact_bytes // MB,
            "max_upload_mb": self.max_upload_mb,
            "max_total_storage_mb": self.max_total_storage_mb,
            "port": self.port,
            "log_level": self.log_level,
        }


# Backwards-compatible alias: existing code/tests refer to `Config`.
Config = Settings


@functools.cache
def get_settings() -> Settings:
    return Settings()
