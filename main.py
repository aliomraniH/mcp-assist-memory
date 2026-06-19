"""Replit / production entrypoint: Postgres-backed FastAPI app on $PORT."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from assist_memory.app import main

if __name__ == "__main__":
    main()
