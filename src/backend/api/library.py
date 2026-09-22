"""Phase 2.9a — Local visual asset library endpoints."""
import shutil
import tempfile
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse

from backend.lib.user_context import multi_tenant_enabled, owner_value
from backend.schemas.library import (
    LibraryBackfillResponse, LibraryClipRead, LibraryImportFileResponse,
    LibraryImportFolderRequest, LibraryImportFolderResponse,
    LibraryListResponse, LibraryRetryTagsResponse, LibraryStats,
)
from backend.services.library_service import LibraryService

router = APIRouter()


def _svc() -> LibraryService:
    return LibraryService()


def _owner(request: Request) -> str | None:
    """多租户下当前用户 id(用于按用户隔离素材);桌面单机模式返回 None = 不隔离。"""
    return owner_value(request) if multi_tenant_enabled() else None


def _clip_owned_or_404(clip, request: Request):
    """多租户下:clip 不属于当前用户就当作不存在(404),防越权看/删别人的素材。"""
    if clip is None:
        raise HTTPException(404, "clip not found")
    if multi_tenant_enabled() and getattr(clip, "user_id", None) != owner_value(request):
        raise HTTPException(404, "clip not found")
    return clip


def _desktop_only(request: Request):
    """读服务器路径/全局操作(导入本地文件夹、扫描全部pipeline、批量重打标)——
    只在桌面单机模式允许;共享云服务器上禁用,避免任意读盘/影响他人。"""
    if multi_tenant_enabled():
        raise HTTPException(403, "not available on the hosted service")


@router.get("/stats", response_model=LibraryStats)
def get_stats(request: Request):
    return _svc().get_stats(user_id=_owner(request))


@router.get("/clips", response_model=LibraryListResponse)
def list_clips(
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(24, ge=1, le=200),
    category: str | None = None,
    tag_status: str | None = None,
    q: str | None = None,
):
    """List clips, paginated. If `q` is set, switches to lexical search
    (only-indexed) ignoring page/page_size and returning up to page_size hits."""
    svc = _svc()
    uid = _owner(request)
    if q:
        rows = svc.search(
            q, top_n=page_size, category=category, only_indexed=True, user_id=uid,
        )
        return LibraryListResponse(
            items=[LibraryClipRead.model_validate(r) for r in rows],
            total=len(rows), page=1, page_size=page_size,
        )
    rows, total = svc.list_paginated(
        page=page, page_size=page_size,
        category=category, tag_status=tag_status, user_id=uid,
    )
    return LibraryListResponse(
        items=[LibraryClipRead.model_validate(r) for r in rows],
        total=total, page=page, page_size=page_size,
    )


@router.get("/clips/{clip_id}", response_model=LibraryClipRead)
def get_clip(clip_id: str, request: Request):
    return _clip_owned_or_404(_svc().get(clip_id), request)


@router.get("/clips/{clip_id}/video")
def stream_video(clip_id: str, request: Request):
    c = _clip_owned_or_404(_svc().get(clip_id), request)
    if not c.local_path:
        raise HTTPException(404, "clip not found")
    p = Path(c.local_path)
    if not p.exists():
        raise HTTPException(404, "video file missing on disk")
    return FileResponse(
        path=str(p), media_type="video/mp4",
        filename=p.name, headers={"Accept-Ranges": "bytes"},
    )


@router.get("/clips/{clip_id}/thumbnail")
def stream_thumbnail(clip_id: str, request: Request):
    c = _clip_owned_or_404(_svc().get(clip_id), request)
    if not c.thumbnail_path:
        raise HTTPException(404, "thumbnail not available")
    p = Path(c.thumbnail_path)
    if not p.exists():
        raise HTTPException(404, "thumbnail file missing")
    return FileResponse(path=str(p), media_type="image/jpeg")


@router.delete("/clips/{clip_id}", status_code=204)
def delete_clip(clip_id: str, request: Request):
    _clip_owned_or_404(_svc().get(clip_id), request)  # 越权删除防护
    if not _svc().delete(clip_id):
        raise HTTPException(404, "clip not found")


@router.post("/clips/{clip_id}/retag", response_model=LibraryClipRead)
def retag(clip_id: str, request: Request):
    svc = _svc()
    _clip_owned_or_404(svc.get(clip_id), request)
    if not svc.retag(clip_id):
        raise HTTPException(404, "clip not found")
    # Wake worker so retagging happens promptly
    try:
        from backend.workers.background_worker import get_background_worker
        get_background_worker().notify_new_work()
    except Exception:
        pass
    c = svc.get(clip_id)
    return c


@router.post("/retry-failed-tags", response_model=LibraryRetryTagsResponse)
def retry_failed_tags(request: Request):
    _desktop_only(request)
    svc = _svc()
    reset_count = svc.retry_failed_tags()
    if reset_count:
        try:
            from backend.workers.background_worker import get_background_worker
            get_background_worker().notify_new_work()
        except Exception:
            pass
    return LibraryRetryTagsResponse(reset_count=reset_count)


@router.post("/scan-pipelines", response_model=LibraryBackfillResponse)
def scan_pipelines(request: Request):
    """Backfill: walk ~/.media-buddy-oss/pipelines/, ingest every project's
    selected footage clips. Idempotent — already-ingested clips are skipped."""
    _desktop_only(request)
    result = _svc().backfill_from_pipelines()
    # Wake the worker so it starts tagging the freshly ingested clips
    try:
        from backend.workers.background_worker import get_background_worker
        get_background_worker().notify_new_work()
    except Exception:
        pass
    return LibraryBackfillResponse(**result)


@router.post("/import-folder", response_model=LibraryImportFolderResponse)
def import_folder(req: LibraryImportFolderRequest, request: Request):
    """Phase 2.9c — walk a user-supplied local folder and ingest all videos.

    Imports get source='local_import' and tag_status='pending', so the
    BackgroundWorker picks them up for Qwen3-VL tagging in idle time.
    Files are COPIED — user's original folder is untouched.
    """
    _desktop_only(request)
    folder = Path(req.folder_path).expanduser()
    if not folder.exists() or not folder.is_dir():
        return LibraryImportFolderResponse(
            scanned=0, ingested=0, skipped=0,
            error=f"folder not found: {folder}",
        )
    result = _svc().import_local_folder(folder, recursive=req.recursive)
    try:
        from backend.workers.background_worker import get_background_worker
        get_background_worker().notify_new_work()
    except Exception:
        pass
    return LibraryImportFolderResponse(
        scanned=result.get("scanned", 0),
        ingested=result.get("ingested", 0),
        skipped=result.get("skipped", 0),
        error=result.get("error"),
    )


@router.post("/import-file", response_model=LibraryImportFileResponse)
async def import_file(request: Request, file: UploadFile = File(...)):
    """Phase 2.9c — single-file upload. Mostly for non-Electron contexts;
    Electron uses /import-folder with a path picked via showOpenDialog.

    The uploaded bytes are written to a temp file, then ingested into the
    library. Subject to FastAPI's default upload size limits — large files
    should use /import-folder instead.

    filename so `../../escape.mp4` collapses to `escape.mp4` and stays
    inside the mkdtemp dir. Backend is loopback-only so risk is theoretical,
    but it's a 1-line fix.
    """
    _desktop_only(request)
    raw_name = file.filename or ""
    safe_name = Path(raw_name).name  # strips any path components
    suffix = Path(safe_name).suffix or ".mp4"
    tmp_dir = Path(tempfile.mkdtemp(prefix="library-upload-"))
    tmp_path = tmp_dir / (safe_name or f"upload{suffix}")
    try:
        with open(tmp_path, "wb") as out:
            shutil.copyfileobj(file.file, out)
        clip = _svc().import_local_file(tmp_path, move=True)
        try:
            from backend.workers.background_worker import get_background_worker
            get_background_worker().notify_new_work()
        except Exception:
            pass
        if clip is None:
            return LibraryImportFileResponse(
                clip=None,
                error="not a recognized video file",
            )
        return LibraryImportFileResponse(
            clip=LibraryClipRead.model_validate(clip),
        )
    finally:
        # tmp_dir cleanup — file may have been moved out by ingest()
        try:
            if tmp_path.exists():
                tmp_path.unlink()
            tmp_dir.rmdir()
        except Exception:
            pass
