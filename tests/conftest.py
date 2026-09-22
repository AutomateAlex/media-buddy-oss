"""Pytest session-level isolation: route all backend DB writes to a temp dir.

Critical: this runs at import time of conftest.py, which pytest loads BEFORE
discovering and importing test modules. Setting MEDIA_BUDDY_DATA_DIR here
ensures that when tests do `from backend.database import ...`, the engine
picks up the temp path instead of `~/.media-buddy-oss/media_buddy.db`.

Without this, every test run polluted the user's production Series + Projects
tables (the bug that left 192 stray test Series and 1101 test Projects in
"""
from __future__ import annotations

import atexit
import os
import shutil
import tempfile
from pathlib import Path

# Per-pytest-session temp dir. Reused across all tests in this run so the
# in-memory SQLAlchemy engine + SQLite WAL all point at the same files.
_TEST_DATA_DIR = Path(tempfile.mkdtemp(prefix="media-buddy-tests-"))
os.environ["MEDIA_BUDDY_DATA_DIR"] = str(_TEST_DATA_DIR)

# Phase 2.11g — TestClient hits the in-process FastAPI app. The token
# middleware compares against backend.lib.app_token.get_token(), which
# reads from ~/.media-buddy-oss/.app_token. To avoid touching the user's
# real token we set MEDIA_BUDDY_DISABLE_TOKEN_CHECK in tests; the
# middleware honors this flag.
os.environ["MEDIA_BUDDY_DISABLE_TOKEN_CHECK"] = "1"

# desktop refresh token is present. Tests mock the critic/footage layers,
# so the cloud-auth call has nothing to validate. Bypass the check at
# session start; production code still enforces it because this env var
# is only set inside the test process.
os.environ.setdefault("MEDIA_BUDDY_ALLOW_OFFLINE", "1")


@atexit.register
def _cleanup_test_data_dir() -> None:
    """Best-effort cleanup. Ignore failures (Windows file locks etc.)."""
    try:
        shutil.rmtree(_TEST_DATA_DIR, ignore_errors=True)
    except Exception:
        pass
