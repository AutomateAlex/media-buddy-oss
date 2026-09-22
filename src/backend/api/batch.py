"""Phase 2.7c — Batch endpoints.

Two routers are exposed:
- POST /api/series/{series_id}/batch — kick off N projects within a Series
- GET  /api/batch/{batch_run_id}     — poll progress
- GET  /api/batch/{batch_run_id}/projects — list child projects
"""
import logging
import re
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from datetime import datetime, timezone

from backend.database import get_db
from backend.lib.user_context import owner_filter, owner_value, multi_tenant_enabled
from backend.models.batch_run import BatchRun
from backend.models.project import Project
from backend.models.series import Series
from backend.schemas.batch_run import BatchRunCreate, BatchRunRead
from backend.schemas.project import ProjectRead
from backend.services.channel_intelligence_service import (
    ChannelIntelligenceService,
    _dedupe_subject,
)

logger = logging.getLogger(__name__)

# Mounted under /api/series/{sid}/batch — see main.py
series_batch_router = APIRouter()
# Mounted under /api/batch — see main.py
batch_router = APIRouter()


def _extract_batch_title(text: str, fallback: str) -> str:
    # 双语主体提取(认 "主题：" 和 "Topic:",并滤掉中英文样板话)。否则英文 locale 的
    # 批量 prompt 没有中文"主题："→ 旧逻辑掉到"取第一行"→ 全部取到样板首行
    # "Please produce one Short video…" 当标题,导致一批视频同名。复用 _dedupe_subject
    # 作单一真相源,跟选题去重/查重用同一套提取规则。
    subj = _dedupe_subject(text)
    first = next((ln.strip() for ln in str(subj or "").splitlines() if ln.strip()), "")
    title = re.sub(r"\s+", " ", first).strip(" -_#·")
    return (title[:80] or fallback).strip()


@series_batch_router.post(
    "/{series_id}/batch", response_model=BatchRunRead, status_code=201,
)
def create_batch(
    series_id: str,
    payload: BatchRunCreate,
    request: Request,
    db: Session = Depends(get_db),
):
    series = owner_filter(db.query(Series).filter(Series.id == series_id), Series, request).first()
    if not series:
        raise HTTPException(404, "Series not found")

    items: list[tuple[str, str | None, str | None]] = []  # (mode, script_text, prompt)
    if payload.scripts:
        items.extend(("script", s, None) for s in payload.scripts if s.strip())
    if payload.prompts:
        items.extend(("creative", None, p) for p in payload.prompts if p.strip())
    if not items:
        raise HTTPException(400, "Provide at least one non-empty script or prompt")

    if len(items) > series.daily_video_cap:
        raise HTTPException(
            400,
            f"Batch size {len(items)} exceeds Series daily_video_cap "
            f"({series.daily_video_cap})",
        )

    cap = min(payload.daily_cost_cap_usd, series.daily_cost_cap_usd)
    checker = ChannelIntelligenceService()
    for idx, (_mode, script_text, prompt) in enumerate(items, start=1):
        idea = (prompt or script_text or "").strip()
        gate = checker.production_gate(
            db, series_id, idea, script_text or prompt,
            allow_duplicate=payload.allow_duplicate,
        )
        if gate.get("allowed") is False:
            if gate.get("action") == "confirm_duplicate":
                duplicate = gate.get("duplicate") or {}
                raise HTTPException(409, {
                    "error": "channel_duplicate_topic",
                    "script_index": idx - 1,
                    "reason": gate.get("reason"),
                    "duplicate": duplicate,
                    "matches": duplicate.get("matches") or [],
                    "angle_suggestions": gate.get("angle_suggestions") or [],
                })
            fit = gate.get("fit") or {}
            raise HTTPException(422, {
                "error": "channel_position_mismatch",
                "script_index": idx - 1,
                "reason": fit.get("reason"),
                "off_domain_terms": fit.get("off_domain_terms") or [],
            })

    # Points pre-flight: block (with a top-up prompt) before creating any project
    # if the wallet can't cover the batch — pay-as-you-go must not run on credit.
    _preflight_fmt = (payload.output_format or series.output_format or "youtube_landscape").strip()

    # 批次级出片设置:payload 覆盖 > series 默认 > 兜底。套用到本批每条 Project。
    batch_output_format = _preflight_fmt
    batch_tts_provider = payload.tts_provider or series.tts_provider
    batch_output_language = (
        payload.output_language or getattr(series, "output_language", None) or "zh"
    )
    # 字幕语言(独立于配音):None → 出片时跟随 output_language。套用到本批每条 Project。
    batch_subtitle_language = getattr(payload, "subtitle_language", None)
    # 字幕字号缩放系数(客户拖拉杆自选):None → 出片时按 1.0。夹在 [0.5,2.0]。套用到本批每条 Project。
    try:
        _sfs = getattr(payload, "subtitle_font_scale", None)
        batch_subtitle_font_scale = max(0.5, min(2.0, float(_sfs))) if _sfs else None
    except (TypeError, ValueError):
        batch_subtitle_font_scale = None
    # 短视频画面风格(fill 裁剪铺满 / blur 背景虚化)。只对竖屏短视频有意义;
    # 横版/长视频忽略(compose 层只在 portrait 生效)。
    batch_framing_style = (payload.framing_style or "fill").strip().lower()
    # 背景音乐开关:默认关(躲版权投诉)。payload 没给就 False。
    batch_include_music = bool(payload.include_music)
    # 语速挡位:payload 没给就 1.4(快,UI 默认挡)。夹在 [0.7,2.0]。
    try:
        batch_tts_speed = max(0.7, min(2.0, float(payload.tts_speed))) if payload.tts_speed else 1.4
    except (TypeError, ValueError):
        batch_tts_speed = 1.4
    batch_duration_seconds = payload.duration_seconds or (
        600.0 if batch_output_format == "youtube_landscape"
        else 45.0 if batch_output_format in ("youtube_shorts", "tiktok", "instagram_reels")
        else float(series.duration_target_seconds or 60)
    )

    batch = BatchRun(
        user_id=owner_value(request),
        series_id=series_id,
        requested_count=len(items),
        concurrency=payload.concurrency,
        daily_cost_cap_usd=cap,
        tts_provider=batch_tts_provider,
        duration_seconds=batch_duration_seconds,
        output_language=batch_output_language,
        status="pending",
    )
    batch.status = "running"
    db.add(batch)
    db.flush()  # so batch.id is populated

    base_name = (series.name or "Batch")[:60]
    created: list[Project] = []
    skipped_duplicates: list[str] = []
    for idx, (mode, script_text, prompt) in enumerate(items, start=1):
        # 客户给了标题就用他的。⚠️ 他在出稿页**认真改过**这个标题，
        given = None
        _titles = getattr(payload, "titles", None) or []
        if idx - 1 < len(_titles):
            given = str(_titles[idx - 1] or "").strip()[:80] or None
        title = given or _extract_batch_title(
            prompt or script_text or "", f"{base_name} #{idx}"
        )
        p = Project(
            user_id=owner_value(request),
            name=title,
            mode=mode,
            script_text=script_text,
            prompt=prompt,
            output_format=batch_output_format,
            framing_style=batch_framing_style,
            include_music=batch_include_music,
            duration_seconds=batch_duration_seconds,
            tts_provider=batch_tts_provider,
            tts_speed=batch_tts_speed,
            output_language=batch_output_language,
            subtitle_language=batch_subtitle_language,
            subtitle_font_scale=batch_subtitle_font_scale,
            # ⚠️ 只有显式 False 才关字幕。None(没传)保持模型默认 True ——
            #    否则频道页那些不传这个字段的老调用会突然全都没字幕。
            include_subtitles=(
                False if payload.include_subtitles is False else True
            ),
            series_id=series_id,
            batch_run_id=batch.id,
            status="pending",
        )
        db.add(p)
        created.append(p)

    if not created:
        # 整批的主体都被别人占了 → 回滚(含 batch 行),明确告诉前端全是重复。
        db.rollback()
        raise HTTPException(409, {
            "error": "channel_duplicate_topic",
            "reason": "本批选题都已被其他频道/客户做过,已全部跳过。请换新选题。",
            "skipped_duplicates": skipped_duplicates,
        })
    if skipped_duplicates:
        batch.requested_count = len(created)
    db.commit()
    db.refresh(batch)
    # 承载"部分成功"信息回前端(schema 的可选字段;空=全部成功)。
    batch.skipped_duplicates = skipped_duplicates or None

    # Wake the in-process BackgroundWorker, which drains pending projects.
    from backend.workers.background_worker import get_background_worker
    get_background_worker().notify_new_work()
    return batch


@batch_router.get("/{batch_run_id}", response_model=BatchRunRead)
def get_batch(batch_run_id: str, request: Request, db: Session = Depends(get_db)):
    b = owner_filter(db.query(BatchRun).filter(BatchRun.id == batch_run_id), BatchRun, request).first()
    if not b:
        raise HTTPException(404, "BatchRun not found")
    return b


@batch_router.post("/{batch_run_id}/stop", response_model=BatchRunRead)
def stop_batch(batch_run_id: str, request: Request, db: Session = Depends(get_db)):
    b = owner_filter(db.query(BatchRun).filter(BatchRun.id == batch_run_id), BatchRun, request).first()
    if not b:
        raise HTTPException(404, "BatchRun not found")
    if b.status not in ("pending", "running"):
        raise HTTPException(409, f"BatchRun is not running or queued: {b.status}")

    db.query(Project).filter(
        Project.batch_run_id == batch_run_id,
        Project.status.in_(("pending", "running", "paused_auth_required")),
    ).update(
        {"status": "stopped", "current_stage": "stopped"},
        synchronize_session=False,
    )

    # Stopping the batch must also cancel the still-QUEUED render jobs, or the
    # worker keeps rendering (and billing!) videos the user just stopped. Only
    # 'queued' jobs are cancelled here — a job already claimed/running is mid-
    # render and will finish; queued ones (the bulk of a stopped batch) never run.
    try:
        from backend.models.video_job import VideoJob
        project_ids = {
            pid for (pid,) in db.query(Project.id).filter(
                Project.batch_run_id == batch_run_id,
            ).all()
        }
        if project_ids:
            cancelled = 0
            for job in db.query(VideoJob).filter(VideoJob.status == "queued").all():
                pid = (job.input or {}).get("project_id") or job.id
                if pid in project_ids:
                    job.status = "cancelled"
                    job.error_code = "stopped_by_user"
                    job.error_message = "batch stopped by user"
                    job.completed_at = datetime.now(timezone.utc)
                    cancelled += 1
            if cancelled:
                logger.info("stop_batch %s cancelled %d queued render job(s)", batch_run_id, cancelled)
    except Exception:  # noqa: BLE001 — never block the stop on queue cleanup
        logger.exception("stop_batch %s: failed to cancel queued video_jobs", batch_run_id)

    b.status = "stopped"
    b.abort_reason = "stopped by user"
    b.finished_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(b)
    return b


@batch_router.get("/{batch_run_id}/projects", response_model=list[ProjectRead])
def list_batch_projects(batch_run_id: str, request: Request, db: Session = Depends(get_db)):
    b = owner_filter(db.query(BatchRun).filter(BatchRun.id == batch_run_id), BatchRun, request).first()
    if not b:
        raise HTTPException(404, "BatchRun not found")
    return (
        db.query(Project)
        .filter(Project.batch_run_id == batch_run_id)
        .order_by(Project.created_at.asc())
        .all()
    )
