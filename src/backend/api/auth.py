"""Local account status API.

The hosted product pairs the app with a Media Buddy cloud account here. This
source-available build has no account: ``/status`` reports whether provider
keys are configured, and display name / language / avatar are stored locally
so the sidebar widget keeps working.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, File, HTTPException, UploadFile
from pydantic import BaseModel, Field

from backend.database import DATA_DIR
from backend.lib import secret_store

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/auth", tags=["auth"])

AVATAR_PATH = DATA_DIR / "avatar.png"
DISPLAY_NAME_KEY = "media_buddy.display_name"
APP_LANGUAGE_KEY = "media_buddy.app_language"

_LLM_KEYS = ("OPENROUTER_API_KEY",)


def _has_llm_key() -> bool:
    return any((os.environ.get(k) or "").strip() for k in _LLM_KEYS)


class AuthStatusResponse(BaseModel):
    authenticated: bool
    plan: Optional[str] = "local"
    sub_status: Optional[str] = None
    email: Optional[str] = None
    display_name: Optional[str] = None
    avatar_path: Optional[str] = None
    app_language: Optional[str] = None
    maintenance: Optional[dict[str, Any]] = None
    # source-available build: which provider keys are configured (names only)
    configured_keys: list[str] = Field(default_factory=list)
    missing_required_keys: list[str] = Field(default_factory=list)


class DisplayNameRequest(BaseModel):
    display_name: str = Field(min_length=1, max_length=60)


class AppLanguageRequest(BaseModel):
    app_language: str = Field(min_length=2, max_length=16)


@router.get("/status", response_model=AuthStatusResponse)
async def get_status() -> AuthStatusResponse:
    from backend.api.settings import KNOWN_KEYS, _KEY_INDEX

    configured = [k for k in KNOWN_KEYS if (os.environ.get(k) or "").strip()]
    missing = [k for k in KNOWN_KEYS if _KEY_INDEX[k]["required"] and k not in configured]
    return AuthStatusResponse(
        authenticated=_has_llm_key(),
        plan="local",
        sub_status="active" if _has_llm_key() else "no_keys",
        display_name=secret_store.get(DISPLAY_NAME_KEY),
        app_language=secret_store.get(APP_LANGUAGE_KEY),
        avatar_path=str(AVATAR_PATH.resolve()) if AVATAR_PATH.is_file() else None,
        configured_keys=configured,
        missing_required_keys=missing,
    )


@router.post("/display-name")
async def set_display_name(req: DisplayNameRequest) -> dict:
    name = req.display_name.strip()
    if not name:
        raise HTTPException(status_code=400, detail={"error": "empty_name"})
    secret_store.set(DISPLAY_NAME_KEY, name)
    return {"ok": True, "cloud_synced": False, "display_name": name}


@router.post("/app-language")
async def set_app_language(req: AppLanguageRequest) -> dict:
    lang = req.app_language.strip()
    secret_store.set(APP_LANGUAGE_KEY, lang)
    return {"ok": True, "app_language": lang}


@router.get("/avatar")
async def get_avatar():
    from fastapi.responses import FileResponse

    if not AVATAR_PATH.is_file():
        raise HTTPException(404, "no avatar")
    return FileResponse(str(AVATAR_PATH), media_type="image/png")


@router.post("/avatar")
async def upload_avatar(file: UploadFile = File(...)) -> dict:
    data = await file.read()
    if len(data) > 5 * 1024 * 1024:
        raise HTTPException(413, "avatar too large (max 5MB)")
    AVATAR_PATH.parent.mkdir(parents=True, exist_ok=True)
    AVATAR_PATH.write_bytes(data)
    return {"ok": True, "avatar_path": str(AVATAR_PATH.resolve()), "avatar_url": "/api/auth/avatar"}


@router.post("/logout")
async def logout() -> dict:
    """No hosted session to revoke; kept so old clients do not 404."""
    return {"ok": True}
