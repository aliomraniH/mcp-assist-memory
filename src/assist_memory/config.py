"""Environment-variable configuration. Read once at startup."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

MB = 1024 * 1024


@dataclass(frozen=True)
class Config:
    auth_token: str
    data_dir: Path
    max_upload_mb: int = 25
    max_total_storage_mb: int = 500
    port: int = 8000
    log_level: str = "INFO"

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * MB

    @property
    def max_total_storage_bytes(self) -> int:
        return self.max_total_storage_mb * MB

    @property
    def max_zip_decompressed_bytes(self) -> int:
        return 4 * self.max_upload_bytes


def load_config() -> Config:
    token = os.environ.get("MCP_AUTH_TOKEN", "").strip()
    if not token:
        raise RuntimeError(
            "MCP_AUTH_TOKEN environment variable is required; refusing to start"
        )
    return Config(
        auth_token=token,
        data_dir=Path(os.environ.get("DATA_DIR", "./data")),
        max_upload_mb=int(os.environ.get("MAX_UPLOAD_MB", "25")),
        max_total_storage_mb=int(os.environ.get("MAX_TOTAL_STORAGE_MB", "500")),
        port=int(os.environ.get("PORT", "8000")),
        log_level=os.environ.get("LOG_LEVEL", "INFO"),
    )
