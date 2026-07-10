import os

# Tests run in zero-config "open dev" mode (no AUTH_TOKEN, no users) — the same mode
# local `./run.sh` uses. Production leaves ALLOW_OPEN_DEV unset, so auth is enforced.
os.environ.setdefault("ALLOW_OPEN_DEV", "1")
