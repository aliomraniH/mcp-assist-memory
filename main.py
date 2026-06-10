"""Replit / production entrypoint: binds 0.0.0.0 on $PORT."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

import uvicorn

from assist_memory.config import load_config
from assist_memory.server import create_app


def main() -> None:
    config = load_config()
    app = create_app(config)
    uvicorn.run(app, host="0.0.0.0", port=config.port)


if __name__ == "__main__":
    main()
