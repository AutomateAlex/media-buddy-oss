"""Pipeline run/status endpoints."""
import logging
import os
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.lib.user_context import owner_filter, owner_value, multi_tenant_enabled
from backend.models.project import Project
from backend.schemas.pipeline import PipelineRunRequest, PipelineStatus, StageStatus
from backend.workers.background_worker import get_background_worker
from backend.lib.progress import read_progress

logger = logging.getLogger(__name__)

logger = logging.getLogger(__name__)
router = APIRouter()
PIPELINE_DIR = Path.home() / ".media-buddy-oss" / "pipelines"


@router.post("/run")
def start_pipeline(req: PipelineRunRequest, request: Request, db: Session = Depends(get_db)):
    """Phase 2.8 — enqueue by setting status='pending' and waking the
    in-process BackgroundWorker. No external broker. Worker picks up the
    next pending Project (oldest first) and runs synchronously."""
    p = owner_filter(db.query(Project).filter(Project.id == req.project_id), Project, request).first()
    if not p:
        raise HTTPException(404, "Project not found")
    if p.status == "running":
        raise HTTPException(409, "Pipeline already running")
    # 🚨 同一条项目已经在队列里 → 直接把那条还回去,别再排一条。
    #       也不该因为余额检查失败。
    # 队列安全阀:防单用户灌爆队列(营销爆量时尤其重要)。超上限给友好提示而非硬错。
    # Points pre-flight: block (with a top-up prompt) before producing if the
    # wallet can't cover this video. No-op on desktop.
    p.status = "pending"
    db.commit()
    get_background_worker().notify_new_work()
    return {"project_id": p.id, "status": "queued"}


@router.get("/status/{project_id}", response_model=PipelineStatus)
def get_status(project_id: str, request: Request, db: Session = Depends(get_db)):
    p = owner_filter(db.query(Project).filter(Project.id == project_id), Project, request).first()
    if not p:
        raise HTTPException(404, "Project not found")

    stages = []
    for stage in (
        "script",
        "voice",
        "assets",
        "music",
        "rough_cut",
        "fine_cut",
        "subtitles",
        "seo",
        "export",
        "compose",
    ):
        cp = read_progress(PIPELINE_DIR, project_id, stage)
        if cp:
            stages.append(
                StageStatus(
                    stage=stage,
                    status=cp.get("status", "unknown"),
                    artifacts=cp.get("artifacts", {}),
                    error=cp.get("error"),
                )
            )

    # 排队中(pending/queued、还没开始渲)→ 给用户队列位置 + 预计等待。
    qpos = qeta = None
    if p.status in ("pending", "queued"):
        qpos, qeta = _queue_position_eta(db, p)

    real_pct = None
    try:
        from backend.models.video_job import VideoJob
        vj = (
            db.query(VideoJob.progress)
            .filter(
                (VideoJob.input.op("->>")("project_id") == project_id)
                | (VideoJob.id == project_id)
            )
            .order_by(VideoJob.created_at.desc())
            .first()
        )
        if vj and isinstance(vj[0], dict):
            _pv = vj[0].get("pct")
            if isinstance(_pv, (int, float)):
                real_pct = float(_pv)
    except Exception:
        real_pct = None

    return PipelineStatus(
        project_id=project_id,
        overall_status=p.status,
        current_stage=p.current_stage,
        stages=stages,
        output_path=p.output_path,
        celery_task_id=p.celery_task_id,
        queue_position=qpos,
        queue_eta_seconds=qeta,
        pct=real_pct,
    )
