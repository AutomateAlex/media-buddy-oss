"""Gateway interface + error types.

In the hosted product this class proxies every AI call through the Media
that calls OpenRouter / ElevenLabs directly with the user's own
keys. ``CloudGateway`` is kept as the base class and for type annotations.
"""
from __future__ import annotations

import threading
from typing import Any, Optional

from .cloud_auth import CloudAuth, NotAuthenticatedError, get_default_auth


class GatewayError(RuntimeError):
    """Base for all gateway errors."""

    def __init__(self, message: str, *, payload: Optional[dict] = None) -> None:
        super().__init__(message)
        self.payload = payload or {}


class QuotaExceededError(GatewayError):
    """402 — kept for call-site compatibility; never raised locally."""


class SubscriptionInactiveError(GatewayError):
    """403 — kept for call-site compatibility; never raised locally."""


class UpstreamError(GatewayError):
    """Provider returned an error."""


class CloudGateway:
    """Abstract surface. Every method is implemented by ``LocalGateway``."""

    def __init__(self, auth: Optional[CloudAuth] = None, base_url: Optional[str] = None) -> None:
        self.auth = auth or get_default_auth()
        self.base_url = (base_url or "local").rstrip("/")

    def is_authenticated(self) -> bool:
        return bool(self.auth.is_authenticated)


    async def llm_completion(self, **kwargs: Any) -> dict[str, Any]:
        raise NotAuthenticatedError("not implemented in the base gateway")

    async def get_quota(self) -> dict[str, Any]:
        raise NotAuthenticatedError("not implemented in the base gateway")

    async def tts_synthesize(self, **kwargs: Any) -> tuple[bytes, dict[str, Any]]:
        raise NotAuthenticatedError("not implemented in the base gateway")

    async def tts_voices(self) -> dict[str, Any]:
        raise NotAuthenticatedError("not implemented in the base gateway")

    async def settle_video_cost(self, **kwargs: Any) -> dict[str, Any]:
        return {"ok": True, "charged": 0, "note": "local build: nothing to settle"}

    async def get_wallet_minutes(self) -> dict[str, Any]:
        return {"unlimited": True, "minutes": None}


_default_gateway: Optional[CloudGateway] = None
_default_gateway_lock = threading.Lock()


def get_default_gateway() -> CloudGateway:
    """Return the process-wide gateway singleton (a ``LocalGateway``)."""
    global _default_gateway
    if _default_gateway is None:
        with _default_gateway_lock:
            if _default_gateway is None:
                from .local_gateway import LocalGateway  # lazy: avoids import cycle

                _default_gateway = LocalGateway()
    return _default_gateway


def reset_default_gateway() -> None:
    """Test-only helper."""
    global _default_gateway
    with _default_gateway_lock:
        _default_gateway = None
