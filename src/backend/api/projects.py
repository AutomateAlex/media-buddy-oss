"""CRUD endpoints for projects."""
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ValidationError
from fastapi.responses import FileResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.lib.user_context import owner_filter, owner_value, multi_tenant_enabled
from backend.models.chunk import Chunk, Shot
from backend.models.project import Project
from backend.models.series import Series
from backend.schemas.chunk import ChunkRead, ChunkReplaceRequest
from backend.schemas.project import ProjectCreate, ProjectRead
from backend.services.channel_intelligence_service import ChannelIntelligenceService
from backend.workers.background_worker import get_background_worker
from backend.lib.progress import read_progress

router = APIRouter()
PIPELINE_DIR = Path.home() / ".media-buddy-oss" / "pipelines"
logger = logging.getLogger(__name__)


class ProjectSeriesAssign(BaseModel):
    series_id: str | None = None

# Project columns that a Series can default-supply when not set on the request.
_SERIES_INHERITABLE_FIELDS = ("output_format", "tts_provider", "output_language")
# Hardcoded fallbacks when neither request nor Series provides the value.
_FIELD_FALLBACKS = {
    "output_format": "youtube_landscape",
    "tts_provider": "azure_yunyang",
    "output_language": "zh",
}


def _reconcile_finished_project(p: Project, db: Session) -> Project:
    """Repair stale DB status when a direct/local run wrote completed progress."""
    if p.status not in ("pending", "running", "stopped"):
        return p

    export = read_progress(PIPELINE_DIR, p.id, "export")
    compose = read_progress(PIPELINE_DIR, p.id, "compose")
    done = export if export and export.get("status") == "completed" else compose
    if not done or done.get("status") != "completed":
        return p

    report = (done.get("artifacts") or {}).get("render_report") or {}
    output_path = report.get("output_path")
    if not output_path:
        return p
    out = Path(str(output_path))
    if not out.exists():
        return p

    p.status = "completed"
    p.current_stage = None
    p.output_path = str(out)
    p.pipeline_dir = str(PIPELINE_DIR / p.id)
    db.commit()
    db.refresh(p)
    return p


@router.get("/", response_model=list[ProjectRead])
def list_projects(
    request: Request,
    series_id: str | None = Query(default=None),
    unassigned: bool = Query(default=False),
    db: Session = Depends(get_db),
):
    q = owner_filter(db.query(Project), Project, request)
    if series_id:
        q = q.filter(Project.series_id == series_id)
    elif unassigned:
        q = q.filter(Project.series_id.is_(None))
    projects = q.order_by(Project.created_at.desc()).all()

    # Validate row by row and drop the ones that fail. FastAPI's response_model
    # validates the list as a unit, so ONE malformed row (a NULL where the schema
    # wants a bool, say) turns the whole call into a 500 — and the user sees "all my
    # projects are gone" instead of one missing card. Losing one row beats losing all
    # of them; the skip is logged loudly so the bad data still gets found.
    out: list[ProjectRead] = []
    for p in projects:
        try:
            out.append(ProjectRead.model_validate(_reconcile_finished_project(p, db)))
        except ValidationError:
            logger.exception(
                "project %s failed response validation — skipping it so the rest of "
                "the list still renders. Fix the row.", getattr(p, "id", "?"),
            )
    return out


@router.post("/", response_model=ProjectRead, status_code=201)
def create_project(data: ProjectCreate, request: Request, db: Session = Depends(get_db)):
    payload = data.model_dump()
    explicit = data.model_dump(exclude_unset=True)
    # ── 🚨 幂等:同一个键重发只会得到同一条项目 ────────────────────────
    #
    # `queue_full`,他以为没发出去 → 又建了一遍 → 同一份稿子两条片、扣两次钱。
    #
    # 做法:项目 id = uuid5(owner + 客户端给的键)。重发同键 → 撞到同一个主键 →
    # 直接把已有那条还回去。**不需要新加数据库字段。**
    #
    # ⚠️ 必须**拌上 owner**:光用裸键的话,别人拿到/猜到你的键就能撞你的主键 ——
    #    既会 500,也是一个跨账号试探的口子。
    # ⚠️ 只挡【同一次提交被重发】。客户过几分钟主动再建一条是**故意的**,照样放行。
    # ⚠️ 不传键 = 行为和以前逐字节一样(桌面端老客户端不受影响)。
    idem = str(payload.pop("idempotency_key", None) or "").strip()
    forced_id: str | None = None
    if idem:
        import uuid as _uuid
        forced_id = str(_uuid.uuid5(_uuid.NAMESPACE_URL,
                                    "mb:project:%s:%s" % (owner_value(request), idem)))
        existing = owner_filter(
            db.query(Project).filter(Project.id == forced_id), Project, request
        ).first()
        if existing is not None:
            # 重放命中。**必须在这里就返回** —— 后面的频道守卫会再跑一次
            # (某些路径要花 LLM 的钱),重放不该付第二次。
            logger.info("create_project: idempotent replay -> %s", existing.id)
            return existing
    series: Series | None = None
    if payload.get("series_id"):
        series = owner_filter(
            db.query(Series).filter(Series.id == payload["series_id"]), Series, request
        ).first()
        if not series:
            raise HTTPException(404, "Series not found")
        # 出片端点服务于长/短视频编导助手(对话式 + 粘贴文案 verbatim)。
        # 客户想做什么就做什么。allow_duplicate=True 只跳过相似闸,频道定位/内容策略(串台)仍拦。
        ChannelIntelligenceService().validate_or_raise(
            db,
            series.id,
            str(payload.get("name") or payload.get("prompt") or ""),
            str(payload.get("script_text") or payload.get("prompt") or ""),
            allow_duplicate=True,
        )
    # If field NOT explicitly set on request and Series has it, override the
    # Pydantic default with Series's value (Series wins over schema default).
    if series is not None:
        for field in _SERIES_INHERITABLE_FIELDS:
            if field not in explicit:
                series_val = getattr(series, field, None)
                if series_val:
                    payload[field] = series_val
    project = Project(**payload)
    project.user_id = owner_value(request)
    if forced_id:
        project.id = forced_id
    db.add(project)
    try:
        db.commit()
    except IntegrityError:
        # ⚠️ 两个请求同时进来、都通过了上面的存在性检查,第二条插入才撞主键。
        #    这正是要挡的「双击」场景 —— 不能让它变成 500。
        db.rollback()
        if forced_id:
            dup = owner_filter(
                db.query(Project).filter(Project.id == forced_id), Project, request
            ).first()
            if dup is not None:
                logger.info("create_project: idempotent race -> %s", dup.id)
                return dup
        raise
    db.refresh(project)
    return project


@router.get("/download-zip")
def download_zip(request: Request, ids: str = Query(...), db: Session = Depends(get_db)):
    """Bundle several finished videos into one streamed ZIP (STORED, no recompress).

    Registered BEFORE `GET /{project_id}` so 'download-zip' is not read as an id."""
    import io
    import zipfile
    from datetime import datetime, timezone
    from fastapi.responses import StreamingResponse

    id_list = [x for x in (ids or "").split(",") if x]
    if not id_list:
        raise HTTPException(422, "ids is empty")
    if len(id_list) > 50:
        raise HTTPException(400, "Too many videos in one download (max 50)")
    projects = (
        owner_filter(db.query(Project).filter(Project.id.in_(id_list)), Project, request)
        .filter(Project.status == "completed", Project.output_path.isnot(None))
        .all()
    )
    items: list[tuple[str, Path]] = []
    seen: dict[str, int] = {}
    for p in projects:
        out = Path(str(p.output_path))
        if not out.is_file():
            continue
        name = _download_filename(p.name)
        if name in seen:
            seen[name] += 1
            name = f"{name[:-4]} ({seen[name]}).mp4"
        else:
            seen[name] = 0
        items.append((name, out))
    if not items:
        raise HTTPException(404, "No downloadable videos in selection")

    now = datetime.now(timezone.utc)
    for p in projects:
        if p.downloaded_at is None:
            p.downloaded_at = now
    db.commit()

    def _gen():
        class _Sink(io.RawIOBase):
            def __init__(self):
                super().__init__(); self.buf = bytearray()
            def writable(self): return True
            def write(self, b): self.buf += b; return len(b)
        sink = _Sink()
        with zipfile.ZipFile(sink, "w", zipfile.ZIP_STORED) as zf:
            for arcname, path in items:
                with path.open("rb") as fh, zf.open(zipfile.ZipInfo(arcname), "w", force_zip64=True) as dst:
                    while True:
                        chunk = fh.read(256 * 1024)
                        if not chunk:
                            break
                        dst.write(chunk)
                        if sink.buf:
                            yield bytes(sink.buf); sink.buf.clear()
        if sink.buf:
            yield bytes(sink.buf)

    return StreamingResponse(
        _gen(), media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="media-buddy-videos.zip"'},
    )


@router.get("/{project_id}", response_model=ProjectRead)
def get_project(project_id: str, request: Request, db: Session = Depends(get_db)):
    p = owner_filter(db.query(Project).filter(Project.id == project_id), Project, request).first()
    if not p:
        raise HTTPException(404, "Project not found")
    return _reconcile_finished_project(p, db)


@router.patch("/{project_id}/series", response_model=ProjectRead)
def assign_project_series(
    project_id: str,
    body: ProjectSeriesAssign,
    request: Request,
    db: Session = Depends(get_db),
):
    p = owner_filter(db.query(Project).filter(Project.id == project_id), Project, request).first()
    if not p:
        raise HTTPException(404, "Project not found")
    if body.series_id:
        s = owner_filter(db.query(Series).filter(Series.id == body.series_id), Series, request).first()
        if not s:
            raise HTTPException(404, "Series not found")
    p.series_id = body.series_id
    db.commit()
    db.refresh(p)
    return p


@router.post("/{project_id}/mark-downloaded", response_model=ProjectRead)
def mark_project_downloaded(project_id: str, request: Request, db: Session = Depends(get_db)):
    """客户点下载视频时调一次,给项目打"已下载"时间戳(幂等,只首次设)。
    列表据此显示"已下载"标识。"""
    from datetime import datetime, timezone
    p = owner_filter(db.query(Project).filter(Project.id == project_id), Project, request).first()
    if not p:
        raise HTTPException(404, "Project not found")
    if p.downloaded_at is None:
        p.downloaded_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(p)
    return p


class RetentionIntentBody(BaseModel):
    retention_intent: str  # "keep"(★收藏) | "auto_expire"(默认到期清)


@router.post("/{project_id}/retention-intent", response_model=ProjectRead)
def set_retention_intent(
    project_id: str, body: RetentionIntentBody, request: Request, db: Session = Depends(get_db),
):
    """收藏/取消收藏一条视频(免费,仅改意向)。收藏的视频到期前享聚合续存提醒;
    但收藏≠免费永久 —— 没付费续存,到期照删(清理任务不看这个字段)。"""
    intent = (body.retention_intent or "").strip().lower()
    if intent not in ("keep", "auto_expire"):
        raise HTTPException(422, "retention_intent must be 'keep' or 'auto_expire'")
    p = owner_filter(db.query(Project).filter(Project.id == project_id), Project, request).first()
    if not p:
        raise HTTPException(404, "Project not found")
    p.retention_intent = intent
    db.commit()
    db.refresh(p)
    return p


class ExtendRetentionBatchBody(BaseModel):
    project_ids: list[str]
    idempotency_key: str


@router.delete("/{project_id}", status_code=204)
def delete_project(project_id: str, request: Request, db: Session = Depends(get_db)):
    p = owner_filter(db.query(Project).filter(Project.id == project_id), Project, request).first()
    if not p:
        raise HTTPException(404, "Project not found")
    db.delete(p)
    db.commit()


@router.post("/{project_id}/stop", response_model=ProjectRead)
def stop_project(project_id: str, request: Request, db: Session = Depends(get_db)):
    p = owner_filter(db.query(Project).filter(Project.id == project_id), Project, request).first()
    if not p:
        raise HTTPException(404, "Project not found")
    if p.status not in ("pending", "running", "paused_auth_required"):
        raise HTTPException(409, f"Project is not running or queued: {p.status}")
    p.status = "stopped"
    p.current_stage = "stopped"
    db.commit()
    db.refresh(p)
    return p


@router.post("/{project_id}/restart", response_model=ProjectRead)
def restart_project(project_id: str, request: Request, db: Session = Depends(get_db)):
    p = owner_filter(db.query(Project).filter(Project.id == project_id), Project, request).first()
    if not p:
        raise HTTPException(404, "Project not found")
    if p.status == "running":
        raise HTTPException(409, "Stop the running project before restarting it")
    # uuid5(project_id:final-settlement) 是【按项目稳定】的 —— 原地重渲会命中同一个
    if multi_tenant_enabled() and (p.status == "completed" or p.output_path):
        raise HTTPException(409, detail={
            "error": "already_completed",
            "message": "该项目已出片并计费。restart 仅用于重试失败的出片;要再出一条请新建项目(会按次计费)。",
        })
    pipeline_dir = PIPELINE_DIR / project_id
    if pipeline_dir.exists():
        resolved_root = PIPELINE_DIR.resolve()
        resolved_dir = pipeline_dir.resolve()
        if resolved_root not in resolved_dir.parents and resolved_dir != resolved_root:
            raise HTTPException(500, "Unsafe pipeline directory")
        shutil.rmtree(resolved_dir)
    chunk_ids = [
        row[0] for row in db.query(Chunk.id).filter(Chunk.project_id == project_id).all()
    ]
    if chunk_ids:
        db.query(Shot).filter(Shot.chunk_id.in_(chunk_ids)).delete(synchronize_session=False)
        db.query(Chunk).filter(Chunk.id.in_(chunk_ids)).delete(synchronize_session=False)
    p.status = "pending"
    p.current_stage = None
    p.output_path = None
    p.pipeline_dir = None
    p.celery_task_id = None
    p.cost_usd = 0.0
    db.commit()
    db.refresh(p)
    # (same multi-tenant branch as pipeline /run and batch). Desktop keeps the
    # in-process worker.
    get_background_worker().notify_new_work()
    return p


@router.get("/{project_id}/chunks", response_model=list[ChunkRead])
def list_chunks(project_id: str, request: Request, db: Session = Depends(get_db)):
    """Phase 2.7d — return per-chunk + per-shot detail for the Timeline editor."""
    p = owner_filter(db.query(Project).filter(Project.id == project_id), Project, request).first()
    if not p:
        raise HTTPException(404, "Project not found")
    return (
        db.query(Chunk)
        .filter(Chunk.project_id == project_id)
        .order_by(Chunk.chunk_index.asc())
        .all()
    )


@router.get("/{project_id}/asset-report")
def asset_match_report(project_id: str, request: Request, db: Session = Depends(get_db)):
    """

    Returns per-chunk asset selection metadata so the user can see WHAT
    was chosen, WHICH level / source it came from, WHY (verify reason),
    and whether it was a fallback or a strong match. Drives the "Asset
    Report" panel in the desktop UI.
    """
    p = owner_filter(db.query(Project).filter(Project.id == project_id), Project, request).first()
    if not p:
        raise HTTPException(404, "Project not found")

    chunks = (
        db.query(Chunk)
        .filter(Chunk.project_id == project_id)
        .order_by(Chunk.chunk_index.asc())
        .all()
    )
    report = []
    for c in chunks:
        # one shot per chunk for now (multi-shot is v2.2)
        shot = next(
            (s for s in (c.shots or []) if s.shot_index == 0), None,
        )
        if shot is None:
            report.append({
                "chunk_id": c.id,
                "chunk_index": c.chunk_index,
                "sentence": c.sentence,
                "status": c.status,
                "shot": None,
            })
            continue
        report.append({
            "chunk_id": c.id,
            "chunk_index": c.chunk_index,
            "sentence": c.sentence,
            "status": c.status,
            "shot": {
                "shot_index":             shot.shot_index,
                "director_intent":        shot.director_intent or "",
                "selected_prompt":        shot.selected_prompt or "",
                "selected_asset_url":     shot.selected_source_url or "",
                "selected_asset_source":  shot.selected_source or "",
                "selected_asset_type":    shot.asset_type or "video",
                "selected_level":         shot.used_level or "",
                "selected_local_path":    shot.selected_local_path or "",
                "score":                  shot.mm_critic_score or 0.0,
                "confidence":             shot.confidence or 0.0,
                "verify_reason":          shot.verify_reason or "",
                "is_secondary_source": (
                    bool(shot.selected_source)
                    and shot.selected_source != "library"
                    and not (shot.selected_source or "").startswith("local")
                ),
                "is_image_motion":        (shot.asset_type or "") == "image",
                "motion_type":            shot.motion_type or "",
                "fallback_reason":        shot.fallback_reason or "",
                "cascade_depth":          shot.cascade_depth or 3,
                "used_wave":              shot.used_wave,
            },
        })
    return {"project_id": project_id, "chunks": report}


@router.post("/{project_id}/chunks/{chunk_id}/replace")
def replace_chunk(
    project_id: str,
    chunk_id: str,
    body: ChunkReplaceRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    """Phase 2.7d — re-run a single chunk with an optional prompt override.

    Strategy:
      - Reset chunk + its shots to pending
      - Stash the override prompt in chunk.extra so the orchestrator can
        prefer it over LLM-generated queries when the next pipeline runs
      - Return a hint that the user should re-run the full pipeline to
        produce a new MP4. Partial re-render of just one chunk is left for
        a follow-up release.
    """
    p = owner_filter(db.query(Project).filter(Project.id == project_id), Project, request).first()
    if not p:
        raise HTTPException(404, "Project not found")
    chunk = db.query(Chunk).filter(
        Chunk.id == chunk_id, Chunk.project_id == project_id,
    ).first()
    if not chunk:
        raise HTTPException(404, "Chunk not found")

    extra = dict(chunk.extra or {})
    if body.prompt is not None:
        extra["override_query"] = body.prompt.strip()
    extra["replace_strategy"] = body.strategy
    chunk.extra = extra
    chunk.status = "pending"
    chunk.retry_count = 0
    chunk.text_critic_avg = None
    chunk.mm_critic_avg = None
    for sh in chunk.shots:
        sh.status = "pending"
        sh.text_critic_score = None
        sh.mm_critic_score = None
        sh.selected_local_path = None
        sh.selected_source = None
    db.commit()
    return {
        "status": "queued",
        "detail": (
            "Chunk reset to pending. Re-run the full pipeline (POST "
            "/api/pipeline/run) to apply the override; partial re-render "
            "of a single chunk into the final MP4 is not yet automated."
        ),
        "chunk_id": chunk.id,
    }


def _download_filename(name: str | None) -> str:
    """把视频标题清成安全的下载文件名(去掉文件系统非法字符、压空白、限长)。
    批量下载时客户一眼能认出是哪条,不再是一串 uuid。"""
    import re
    base = re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", (name or "video")).strip()
    base = re.sub(r"\s+", " ", base)[:80].strip() or "video"
    return base + ".mp4"


@router.get("/{project_id}/output")
def stream_output(
    project_id: str, request: Request, db: Session = Depends(get_db),
    download: int = Query(default=0),
):
    """Serve the rendered MP4 so <video> tags can load it without file:// hacks.
    download=1 → force a named-file download (used by single + batch download)."""
    p = owner_filter(db.query(Project).filter(Project.id == project_id), Project, request).first()
    if not p or not p.output_path:
        raise HTTPException(404, "Output not found")
    out = Path(p.output_path)
    if not out.exists():
        raise HTTPException(404, f"File missing on disk: {out.name}")
    if download:
        # 本地(桌面)下载:FileResponse 带 filename 会自动设 Content-Disposition attachment。
        return FileResponse(path=str(out), media_type="video/mp4", filename=_download_filename(p.name))
    # FileResponse handles Range requests automatically for video seek/scrub
    return FileResponse(
        path=str(out),
        media_type="video/mp4",
        filename=out.name,
        headers={"Accept-Ranges": "bytes"},
    )
