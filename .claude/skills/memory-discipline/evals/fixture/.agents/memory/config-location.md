# Config location

Date: 2026-06-02

CLI settings live in `settings.ini` at the repo root, section `[cli]`:

    [cli]
    verbose = false

Add new flags there; the CLI reads it with `configparser` at startup.
