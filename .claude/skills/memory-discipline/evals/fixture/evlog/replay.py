"""Rebuild the projection from the append-only event log."""
import json
from pathlib import Path

LOG_PATH = Path(__file__).resolve().parent.parent / "events.jsonl"


def read_events():
    """Return events as a list of dicts, in the order they appear in the file."""
    with LOG_PATH.open() as f:
        return [json.loads(line) for line in f if line.strip()]


def rebuild():
    """Fold the event log into a {key: value} projection dict."""
    projection = {}
    for event in read_events():
        if event["type"] == "set":
            projection[event["key"]] = event["value"]
        elif event["type"] == "delete":
            projection.pop(event["key"], None)
    return projection
