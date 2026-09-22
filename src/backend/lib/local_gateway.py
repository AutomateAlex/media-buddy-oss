"""

returns one of these) but every call goes straight to the provider with a key
from the environment / Settings page:

- ``llm_completion``  → OpenRouter (``OPENROUTER_API_KEY``); text *and*
  multimodal (image_url) messages are passed through unchanged.
- ``tts_synthesize``  → ElevenLabs (``MEDIA_BUDDY_ELEVENLABS_API_KEY`` or
  ``ELEVENLABS_API_KEY``), ``/with-timestamps`` so subtitle alignment works.

"""
from __future__ import annotations

import asyncio
import base64
import logging
import os
import threading
from typing import Any, Optional

import httpx

from .cloud_auth import NotAuthenticatedError, get_default_auth
from .cloud_gateway import CloudGateway, GatewayError, UpstreamError

logger = logging.getLogger(__name__)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
ELEVENLABS_URL = "https://api.elevenlabs.io/v1/text-to-speech"


def _env(*names: str) -> str:
    for n in names:
        v = (os.environ.get(n) or "").strip()
        if v:
            return v
    return ""


class LocalGateway(CloudGateway):
    """Same interface as ``CloudGateway``; providers are called directly."""

    DEFAULT_TEXT_MODEL = "anthropic/claude-haiku-4-5"

    def __init__(self) -> None:  # noqa: D401 - intentionally not calling super()
        self.auth = get_default_auth()
        self.base_url = "local"
        self._jobs: dict[str, dict[str, Any]] = {}
        self._jobs_lock = threading.Lock()

    # ------------------------------------------------------------ helpers
    def is_authenticated(self) -> bool:
        return bool(_env("OPENROUTER_API_KEY"))

    async def _post_json(self, url: str, *, json: dict[str, Any], headers: dict[str, str], timeout: float) -> tuple[int, Any]:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, json=json, headers=headers)
        try:
            payload: Any = resp.json()
        except Exception:
            payload = {"raw": resp.text[:500]}
        return resp.status_code, payload

    @staticmethod
    def _raise_for_status(status: int, payload: Any, *, provider: str) -> None:
        if status == 200:
            return
        detail = payload
        if isinstance(payload, dict):
            err = payload.get("error")
            if isinstance(err, dict):
                detail = err.get("message") or err
            elif err:
                detail = err
        if status in (401, 403):
            raise NotAuthenticatedError(f"{provider} rejected the API key ({status}): {detail}")
        raise UpstreamError(
            f"{provider} returned {status}: {detail}",
            payload=payload if isinstance(payload, dict) else {"raw": str(payload)},
        )

    # ------------------------------------------------------------ LLM
    async def llm_completion(
        self,
        *,
        messages: list[dict[str, Any]],
        idempotency_key: str,
        max_tokens: Optional[int] = None,
        model_id: Optional[str] = None,
        cache_system: bool = False,
        project_id: Optional[str] = None,
    ) -> dict[str, Any]:
        key = _env("OPENROUTER_API_KEY")
        if not key:
            raise NotAuthenticatedError("OPENROUTER_API_KEY is not set")
        model = model_id or _env("MEDIA_BUDDY_DEFAULT_MODEL") or self.DEFAULT_TEXT_MODEL
        body: dict[str, Any] = {"model": model, "messages": messages}
        if max_tokens is not None:
            body["max_tokens"] = int(max_tokens)
        if cache_system and messages and messages[0].get("role") == "system" and isinstance(messages[0].get("content"), str):
            body["messages"] = [
                {"role": "system", "content": [{"type": "text", "text": messages[0]["content"], "cache_control": {"type": "ephemeral"}}]},
                *messages[1:],
            ]
        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }
        status, payload = await self._post_json(OPENROUTER_URL, json=body, headers=headers, timeout=180.0)
        self._raise_for_status(status, payload, provider="OpenRouter")
        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise GatewayError(f"OpenRouter response missing content: {payload}") from e
        usage = payload.get("usage") or {}
        cost = usage.get("cost") or usage.get("total_cost") or 0.0
        return {
            "job_id": idempotency_key,
            "content": content if isinstance(content, str) else str(content),
            "cost_eur": float(cost or 0.0),
            "model_used": payload.get("model") or model,
            "usage": usage,
        }

    async def get_quota(self) -> dict[str, Any]:
        return {"plan": "local", "sub_status": "active", "unlimited": True, "used_eur": 0.0, "limit_eur": None, "resets_at": None}

    async def get_wallet_minutes(self) -> dict[str, Any]:
        return {"unlimited": True, "minutes": None, "points": None}

    async def settle_video_cost(self, **kwargs: Any) -> dict[str, Any]:
        return {"ok": True, "charged": 0, "note": "local build: you pay your providers directly"}

    # ------------------------------------------------------------ TTS
    async def tts_synthesize(
        self,
        *,
        text: str,
        voice_id: str,
        idempotency_key: str,
        provider: str = "elevenlabs",
        model_id: Optional[str] = None,
        with_timestamps: bool = False,
        speed: float = 1.0,
        project_id: Optional[str] = None,
    ) -> tuple[bytes, dict[str, Any]]:
        if provider not in ("elevenlabs", "", None):
            raise NotAuthenticatedError(f"TTS provider {provider!r} is not available in the local build (use qwen_* or edge_* voices)")
        key = _env("MEDIA_BUDDY_ELEVENLABS_API_KEY", "ELEVENLABS_API_KEY")
        if not key:
            raise NotAuthenticatedError("ELEVENLABS_API_KEY is not set")
        speed = max(0.7, min(1.2, float(speed or 1.0)))
        body: dict[str, Any] = {
            "text": text,
            "model_id": model_id or "eleven_multilingual_v2",
            "voice_settings": {"stability": 0.5, "similarity_boost": 0.75, "speed": speed},
        }
        headers = {"xi-api-key": key, "Content-Type": "application/json"}
        meta: dict[str, Any] = {"job_id": idempotency_key, "cost_eur": "0"}
        if with_timestamps:
            status, payload = await self._post_json(f"{ELEVENLABS_URL}/{voice_id}/with-timestamps", json=body, headers=headers, timeout=180.0)
            self._raise_for_status(status, payload, provider="ElevenLabs")
            if not isinstance(payload, dict):
                raise UpstreamError("ElevenLabs returned a non-JSON timestamp payload")
            meta["alignment"] = payload.get("alignment")
            meta["normalized_alignment"] = payload.get("normalized_alignment")
            return base64.b64decode(payload.get("audio_base64") or ""), meta
        async with httpx.AsyncClient(timeout=180.0) as client:
            resp = await client.post(f"{ELEVENLABS_URL}/{voice_id}", json=body, headers={**headers, "Accept": "audio/mpeg"})
        if resp.status_code != 200:
            try:
                payload = resp.json()
            except Exception:
                payload = {"raw": resp.text[:500]}
            self._raise_for_status(resp.status_code, payload, provider="ElevenLabs")
        return resp.content, meta

    async def tts_voices(self) -> dict[str, Any]:
        """Not used by the local ``/api/tts/voices`` route (it reads the voice registry)."""
        return {"model": "local", "voices": []}


