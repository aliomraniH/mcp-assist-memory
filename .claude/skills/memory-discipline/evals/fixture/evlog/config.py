"""CLI settings. Migrated from settings.ini to config.json on 2026-07-30."""
import json
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"


def load():
    return json.loads(CONFIG_PATH.read_text())
