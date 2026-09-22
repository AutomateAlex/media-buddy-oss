"""Phase 2.9b — Audio library endpoints (BGM tracks)."""
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse

from backend.lib.user_context import multi_tenant_enabled, owner_value
from backend.schemas.library_audio import (
    LibraryAudioListResponse, LibraryAudioRead, LibraryAudioStats,
)
from backend.services.audio_library_service import AudioLibraryService

router = APIRouter()


def _svc() -> AudioLibraryService:
    return AudioLibraryService()


def _owner(request: Request) -> str | None:
    return owner_value(request) if multi_tenant_enabled() else None


def _track_owned_or_404(track, request: Request):
    """多租户下:track 不属于当前用户就当作不存在(404),防越权看/删别人的音频。"""
    if track is None:
        raise HTTPException(404, "track not found")
    if multi_tenant_enabled() and getattr(track, "user_id", None) != owner_value(request):
        raise HTTPException(404, "track not found")
    return track


@router.get("/stats", response_model=LibraryAudioStats)
def get_stats(request: Request):
    return _svc().get_stats(user_id=_owner(request))


@router.get("/tracks", response_model=LibraryAudioListResponse)
def list_tracks(
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(24, ge=1, le=200),
    mood: str | None = None,
    energy: str | None = None,
    q: str | None = None,
):
    svc = _svc()
    uid = _owner(request)
    if q:
        rows = svc.search(q, mood=mood, energy=energy, top_n=page_size, user_id=uid)
        return LibraryAudioListResponse(
            items=[LibraryAudioRead.model_validate(r) for r in rows],
            total=len(rows), page=1, page_size=page_size,
        )
    rows, total = svc.list_paginated(
        page=page, page_size=page_size, mood=mood, energy=energy, user_id=uid,
    )
    return LibraryAudioListResponse(
        items=[LibraryAudioRead.model_validate(r) for r in rows],
        total=total, page=page, page_size=page_size,
    )


@router.get("/tracks/{track_id}", response_model=LibraryAudioRead)
def get_track(track_id: str, request: Request):
    return _track_owned_or_404(_svc().get(track_id), request)


@router.get("/tracks/{track_id}/audio")
def stream_audio(track_id: str, request: Request):
    r = _track_owned_or_404(_svc().get(track_id), request)
    if not r.local_path:
        raise HTTPException(404, "track not found")
    p = Path(r.local_path)
    if not p.exists():
        raise HTTPException(404, "audio file missing on disk")
    media_type = (
        "audio/mpeg" if p.suffix.lower() == ".mp3"
        else "audio/wav" if p.suffix.lower() in (".wav", ".wave")
        else "audio/ogg" if p.suffix.lower() == ".ogg"
        else "application/octet-stream"
    )
    return FileResponse(
        path=str(p), media_type=media_type,
        filename=p.name, headers={"Accept-Ranges": "bytes"},
    )


@router.delete("/tracks/{track_id}", status_code=204)
def delete_track(track_id: str, request: Request):
    _track_owned_or_404(_svc().get(track_id), request)  # 越权删除防护
    if not _svc().delete(track_id):
        raise HTTPException(404, "track not found")
