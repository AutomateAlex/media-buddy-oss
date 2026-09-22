"""Settings API — bring-your-own provider keys.

Keys are stored through ``lib/secret_store`` (OS keychain when available,
otherwise an encrypted-ish local file) and mirrored into ``os.environ`` on
write, so a key saved from the Settings page takes effect immediately without
restarting the backend. Values are never returned to the browser; only
"configured / not configured".
"""
from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from backend.lib import secret_store

router = APIRouter()

# group → [(env var, label, required, hint url)]
KEY_GROUPS: list[dict[str, Any]] = [
    {
        "id": "ai",
        "label": "AI 接口",
        "keys": [
            {"name": "OPENROUTER_API_KEY", "label": "OpenRouter", "required": True,
             "hint": "https://openrouter.ai/keys",
             "what": "脚本、选片、裁判、看图等所有大模型调用都走它（一把 key 通 200+ 模型，含千问、Claude、GPT）"},
        ],
    },
    {
        "id": "tts",
        "label": "配音",
        "keys": [
            {"name": "MEDIA_BUDDY_QWEN_API_KEY", "label": "千问配音（阿里云百炼）", "required": False,
             "hint": "https://dashscope.console.aliyun.com/apiKey",
             "what": "24 个中文音色，默认配音；不填则用免费的 Edge 音色"},
            {"name": "MEDIA_BUDDY_ELEVENLABS_API_KEY", "label": "ElevenLabs", "required": False,
             "hint": "https://elevenlabs.io/", "what": "可选：英文高质量配音（还要设 MEDIA_BUDDY_ENABLE_ELEVENLABS=1）"},
        ],
    },
    {
        "id": "stock",
        "label": "素材站",
        "keys": [
            {"name": "PEXELS_API_KEY", "label": "Pexels", "required": True,
             "hint": "https://www.pexels.com/api/", "what": "免费素材库（视频 + 图片）"},
            {"name": "PIXABAY_API_KEY", "label": "Pixabay", "required": False,
             "hint": "https://pixabay.com/api/docs/", "what": "免费素材库（视频 + 图片 + 音乐）"},
            {"name": "COVERR_API_KEY", "label": "Coverr", "required": False,
             "hint": "https://coverr.co/", "what": "免费素材库"},
            {"name": "FREESOUND_API_KEY", "label": "Freesound", "required": False,
             "hint": "https://freesound.org/apiv2/apply/", "what": "可选：背景音乐备选源"},
        ],
    },
]

KNOWN_KEYS: list[str] = [k["name"] for g in KEY_GROUPS for k in g["keys"]]
_KEY_INDEX = {k["name"]: k for g in KEY_GROUPS for k in g["keys"]}


def _configured(name: str) -> bool:
    return bool((os.environ.get(name) or "").strip())


class KeyWrite(BaseModel):
    value: str = Field(min_length=1, max_length=4000)


@router.get("/keys")
def list_keys() -> dict[str, Any]:
    """Names + configured flag, grouped. Never returns values."""
    groups = []
    for g in KEY_GROUPS:
        groups.append({
            "id": g["id"],
            "label": g["label"],
            "keys": [dict(k, configured=_configured(k["name"])) for k in g["keys"]],
        })
    missing_required = [k for k in KNOWN_KEYS if _KEY_INDEX[k]["required"] and not _configured(k)]
    return {"groups": groups, "missing_required": missing_required}


@router.put("/keys/{name}")
def set_key(name: str, body: KeyWrite) -> dict[str, Any]:
    if name not in _KEY_INDEX:
        raise HTTPException(404, f"unknown key {name}")
    value = body.value.strip()
    if not value or value.startswith("...") or "your-" in value.lower():
        raise HTTPException(422, "that looks like a placeholder, not a key")
    secret_store.set(name, value)  # mirrors into os.environ → takes effect now
    return {"ok": True, "name": name, "configured": True}


@router.delete("/keys/{name}")
def delete_key(name: str) -> dict[str, Any]:
    if name not in _KEY_INDEX:
        raise HTTPException(404, f"unknown key {name}")
    secret_store.delete(name)
    return {"ok": True, "name": name, "configured": False}
