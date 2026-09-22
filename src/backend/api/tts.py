"""TTS API — voice catalogue + local preview for the voice picker.

- Qwen (阿里云百炼) voices when ``MEDIA_BUDDY_QWEN_API_KEY`` is set — the
  production default, 24 Mandarin voices;
- ElevenLabs English voices when ``MEDIA_BUDDY_ELEVENLABS_API_KEY`` is set
  and ``MEDIA_BUDDY_ENABLE_ELEVENLABS=1``;
- free Microsoft Edge voices otherwise (no key needed).
"""
from __future__ import annotations

import hashlib
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from backend.services import voice_registry as _registry
from backend.services.tts_service import EDGE_VOICES, TTSService, _qwen_tts_enabled

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/tts", tags=["tts"])

_QWEN_ZH_CATALOG = _registry.catalog_for("qwen_zh")
_ELEVENLABS_EN_CATALOG = _registry.catalog_for("elevenlabs_en")

_EDGE_META = {
    "edge_xiaoxiao": ("晓晓", "female", "普通话 · 亲切自然（免费）"),
    "edge_xiaoyi": ("晓伊", "female", "普通话 · 清亮活泼（免费）"),
    "edge_yunyang": ("云扬", "male", "普通话 · 新闻播报感（免费）"),
    "edge_yunjian": ("云健", "male", "普通话 · 沉稳有力（免费）"),
    "edge_yunxi": ("云希", "male", "普通话 · 年轻阳光（免费）"),
    "edge_ava": ("Ava", "female", "English · warm narrator (free)"),
    "edge_andrew": ("Andrew", "male", "English · confident presenter (free)"),
}


def _edge_catalog() -> list[dict[str, Any]]:
    rows = []
    for key, vid in EDGE_VOICES.items():
        name, gender, desc = _EDGE_META.get(key, (key, "female", "Microsoft Edge neural voice (free)"))
        lang = "en" if key in ("edge_ava", "edge_andrew") else "zh"
        rows.append({
            "voice_id": vid, "provider_key": key, "display_name": name, "gender": gender,
            "language_hint": lang, "language_label": "English" if lang == "en" else "中文",
            "description": desc, "sample_url": None, "language": lang,
        })
    return rows


def _qwen_key_present() -> bool:
    return bool((os.environ.get("MEDIA_BUDDY_QWEN_API_KEY") or "").strip())


def _elevenlabs_enabled() -> bool:
    on = os.environ.get("MEDIA_BUDDY_ENABLE_ELEVENLABS", "0").strip().lower() in ("1", "true", "yes", "on")
    key = (os.environ.get("MEDIA_BUDDY_ELEVENLABS_API_KEY") or os.environ.get("ELEVENLABS_API_KEY") or "").strip()
    return on and bool(key)


@router.get("/voices")
def list_voices(
    output_format: str = Query("", max_length=40),
    output_language: str = Query("", max_length=8),
    tier: str = Query("", max_length=16),
) -> dict[str, Any]:
    """Voice catalogue for the Studio voice picker. Preview is synthesized on demand."""
    voices: list[dict[str, Any]] = []
    if _qwen_tts_enabled() and _qwen_key_present():
        voices += [dict(v, sample_url=None, language="zh") for v in _QWEN_ZH_CATALOG]
    if _elevenlabs_enabled() and _ELEVENLABS_EN_CATALOG:
        voices += [dict(v, sample_url=None, language="en") for v in _ELEVENLABS_EN_CATALOG]
    voices += _edge_catalog()
    return {"voices": voices, "model": "local"}


@router.get("/preview")
def preview_voice(
    provider: str = Query(..., min_length=1, max_length=80),
    text: str = Query("你好，这是 Media Buddy 的配音试听。", min_length=1, max_length=120),
    speed: float = Query(1.0, ge=0.7, le=2.0),
):
    safe_key = hashlib.sha1(f"{provider}:{speed}:{text}".encode("utf-8")).hexdigest()[:16]
    out_dir = Path(tempfile.gettempdir()) / "media-buddy-tts-preview"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{safe_key}.mp3"
    if not out_path.exists() or out_path.stat().st_size == 0:
        result = TTSService().synthesize(text, str(out_path), provider=provider, speed=speed)
        if not result.success or not result.output_path:
            raise HTTPException(status_code=502, detail=result.error or "tts preview failed")
        out_path = Path(result.output_path)
    return FileResponse(str(out_path), media_type="audio/mpeg", filename=f"{provider}-preview.mp3")
