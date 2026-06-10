"""Best-effort credential-pattern detection: store-but-flag, never reject.

Never log or return the matched content — only the pattern name.
"""

from __future__ import annotations

import re

SECRET_PATTERNS: dict[str, re.Pattern[str]] = {
    "github-token": re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    "github-fine-grained-pat": re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    "anthropic-api-key": re.compile(r"sk-ant-[A-Za-z0-9-]{10,}"),
    "aws-access-key-id": re.compile(r"AKIA[A-Z0-9]{16}"),
    "private-key-block": re.compile(r"-----BEGIN( RSA| EC| OPENSSH)? PRIVATE KEY-----"),
}

POSSIBLE_SECRET_TAG = "possible-secret"


def scan_text(text: str) -> list[str]:
    """Return the names of credential-like patterns found in text."""
    return [name for name, pattern in SECRET_PATTERNS.items() if pattern.search(text)]


def secret_warning(pattern_names: list[str]) -> str:
    return (
        f"value matches a credential-like pattern ({', '.join(pattern_names)}); "
        f"stored with tag '{POSSIBLE_SECRET_TAG}'"
    )
