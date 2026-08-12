# evlog — append-only event log with a rebuildable projection

Events land in `events.jsonl` (append-only; one JSON object per line). The
projection is a plain dict rebuilt from the log:

```
python -m evlog rebuild
```

CLI settings live in `config.json` (see `evlog/config.py`). Run the tests
with `python -m pytest -q`.

Late-arriving events (an `occurred_at` earlier than events already appended)
are a normal part of this system's operation.
