"""Per-install random token to gate the localhost API.

Phase 2.11g — closes the DNS-rebinding / cross-tenant attack surface.
Even though uvicorn binds to 127.0.0.1, a malicious website (or any local
process running under a different user account) could otherwise hit our
API. We require every API request to carry an ``X-MediaBuddy-Token``
header matching a token written to the user's home directory at startup.

Token lifecycle:
  - Generated once with `secrets.token_urlsafe(32)` and persisted to
    ``~/.media-buddy-oss/.app_token`` (file mode 0600 where supported).
  - Re-used across restarts until the file is deleted.
  - Frontend reads it the same way (Electron preload pulls the value from
    ``app.getPath('home')`` and injects it into axios).
"""
from __future__ import annotations

import logging
import os
import secrets
import stat
from pathlib import Path

logger = logging.getLogger(__name__)

_TOKEN_DIR = Path.home() / ".media-buddy-oss"
_TOKEN_FILE = _TOKEN_DIR / ".app_token"

# Header the frontend sends on every request.
HEADER_NAME = "X-MediaBuddy-Token"

# Endpoints that don't require a token. /health is checked by the Electron
# server-manager BEFORE it knows the token (just to confirm uvicorn is up).
# /api/system/status is convenient for debugging "is server alive". Keep
# this list TINY.
PUBLIC_PATHS: frozenset[str] = frozenset({
    "/health",
    "/docs",          # FastAPI's Swagger
    "/openapi.json",
    "/redoc",
})


def _read_or_create() -> str:
    if _TOKEN_FILE.exists():
        try:
            tok = _TOKEN_FILE.read_text(encoding="utf-8").strip()
            if tok:
                return tok
        except Exception as e:
            logger.warning("token file unreadable, regenerating: %s", e)

    _TOKEN_DIR.mkdir(parents=True, exist_ok=True)
    tok = secrets.token_urlsafe(32)
    _TOKEN_FILE.write_text(tok, encoding="utf-8")
    try:
        os.chmod(_TOKEN_FILE, stat.S_IRUSR | stat.S_IWUSR)
    except Exception:
        pass  # Windows ignores chmod; user-account ACL already restricts
    logger.info("Generated new app token at %s", _TOKEN_FILE)
    return tok


# Cache the token in module state — read once at startup, never re-read.
_CACHED_TOKEN: str | None = None


def get_token() -> str:
    global _CACHED_TOKEN
    if _CACHED_TOKEN is None:
        _CACHED_TOKEN = _read_or_create()
    return _CACHED_TOKEN


def token_path() -> Path:
    """Path the frontend reads the token from (Electron preload uses this)."""
    return _TOKEN_FILE


def verify(provided: str | None) -> bool:
    """Constant-time compare against the active token."""
    if not provided:
        return False
    return secrets.compare_digest(provided, get_token())
