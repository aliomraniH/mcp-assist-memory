"""Replit / production entrypoint: binds 0.0.0.0 on $PORT."""

import os
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

import uvicorn

from assist_memory.admin_store import AdminStore
from assist_memory.config import load_config
from assist_memory.observability import setup_logging
from assist_memory.server import create_app


def main() -> None:
    config = load_config()
    setup_logging(config.log_level)

    dsn = os.environ.get("DATABASE_URL", "").strip()
    if dsn:
        # Token is managed in a separate database and surfaced via the /admin
        # dashboard. Seed the first token from MCP_AUTH_TOKEN if one was set.
        admin = AdminStore(dsn)
        admin.ensure_token(seed=config.auth_token or None)
        session_secret = (
            os.environ.get("SESSION_SECRET", "").strip()
            or os.environ.get("ADMIN_PASSWORD", "").strip()
            or secrets.token_urlsafe(32)
        )
        app = create_app(
            config,
            token_provider=admin.get_active_token,
            admin=admin,
            session_secret=session_secret,
        )
    else:
        if not config.auth_token:
            raise RuntimeError(
                "No DATABASE_URL for the admin token store and no MCP_AUTH_TOKEN "
                "fallback set; refusing to start without an auth token."
            )
        app = create_app(config)

    # uvicorn's access log would print full URLs including ?token=...;
    # AccessLogMiddleware provides a query-string-free access log instead.
    uvicorn.run(app, host="0.0.0.0", port=config.port, access_log=False)


if __name__ == "__main__":
    main()
