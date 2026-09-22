"""Local stand-in for the hosted cloud login.

The hosted product pairs the app with a Media Buddy account and keeps JWTs
around. This source-available build has no account: "authenticated" simply
means the user configured an LLM key. Every name the rest of the codebase
imports from here is kept so call sites stay untouched; the token setters
are no-ops.
"""
from __future__ import annotations

import os
import threading
from typing import Optional


class NotAuthenticatedError(RuntimeError):
    """A provider key needed for the requested call is missing."""


class CloudTemporaryUnavailableError(RuntimeError):
    """Kept for call-site compatibility (never raised by the local build)."""


def _has_llm_key() -> bool:
    return any(
        (os.environ.get(k) or "").strip()
        for k in ("OPENROUTER_API_KEY",)
    )


class CloudAuth:
    """Key-presence check with the attribute surface the pipeline expects."""

    def __init__(self, base_url: Optional[str] = None) -> None:
        self.base_url = base_url or "local"
        self._request_token: Optional[str] = None

    @property
    def is_authenticated(self) -> bool:
        return _has_llm_key()

    # --- token plumbing used by the hosted worker/API; harmless no-ops here ---
    def set_worker_token(self, token: Optional[str]) -> None:
        return None

    def set_request_token(self, token: Optional[str]) -> None:
        self._request_token = token

    def current_token(self) -> Optional[str]:
        return self._request_token

    async def authorized_headers(self) -> dict[str, str]:
        raise NotAuthenticatedError("this build has no hosted account; configure provider keys in Settings")

    async def logout(self) -> None:
        return None


_default_auth: Optional[CloudAuth] = None
_default_auth_lock = threading.Lock()


def get_default_auth() -> CloudAuth:
    global _default_auth
    if _default_auth is None:
        with _default_auth_lock:
            if _default_auth is None:
                _default_auth = CloudAuth()
    return _default_auth


def reset_default_auth() -> None:
    global _default_auth
    with _default_auth_lock:
        _default_auth = None
