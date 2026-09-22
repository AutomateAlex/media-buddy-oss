"""Phase 2.7a — Series CRUD endpoints.

A Series is a "channel folder" — locks HardConfig + SoftPrompt for new projects
and accumulates learned_patterns over time. Cache layer remains globally shared
(see models/series.py docstring for the tiered isolation policy).
"""
import json as _json
import logging
import os
import uuid as _uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.lib.user_context import current_user_id, owner_filter, owner_value, multi_tenant_enabled
from backend.models.batch_run import BatchRun
from backend.models.chunk import Chunk, Shot
from backend.models.project import Project
from backend.models.series import Series
from backend.models.studio_session import StudioSession
from backend.schemas.project import ProjectRead
from backend.schemas.series import SeriesCreate, SeriesRead, SeriesUpdate
from backend.services.channel_intelligence_service import ChannelIntelligenceService

logger = logging.getLogger("series")
router = APIRouter()


class ChannelRecommendationQuery(BaseModel):
    content_type: str = "youtube_long"
    limit: int = Field(default=8, ge=1, le=20)


class TopicTitleLocalizationRequest(BaseModel):
    titles: list[str] = Field(min_length=1, max_length=5)
    language: str = "en"


class ChannelCheckRequest(BaseModel):
    idea: str = Field(..., min_length=1, max_length=800)
    script_text: str | None = None


class ChannelDesignRequest(BaseModel):
    description: str = Field(..., min_length=1, max_length=2000)
    output_language: str = "zh"


def _intel() -> ChannelIntelligenceService:
    return ChannelIntelligenceService()


def _owned_series_or_404(series_id: str, db: Session, request: Request) -> Series:
    """Fetch a series scoped to the current user (multi-tenant) or 404."""
    s = owner_filter(
        db.query(Series).filter(Series.id == series_id), Series, request
    ).first()
    if not s:
        raise HTTPException(404, "Series not found")
    return s


@router.get("/", response_model=list[SeriesRead])
def list_series(request: Request, db: Session = Depends(get_db)):
    return owner_filter(db.query(Series), Series, request).order_by(Series.created_at.desc()).all()


@router.post("/", response_model=SeriesRead, status_code=201)
def create_series(data: SeriesCreate, request: Request, db: Session = Depends(get_db)):
    payload = data.model_dump()
    # 防重复建频道:同名(去空格、忽略大小写)的频道本人已有就拦下,别一个题材建两遍。
    name = (payload.get("name") or "").strip()
    if name:
        norm = name.casefold()
        dup = next(
            (s for s in owner_filter(db.query(Series), Series, request).all()
             if (s.name or "").strip().casefold() == norm),
            None,
        )
        if dup is not None:
            raise HTTPException(status_code=409, detail={
                "error": "duplicate_channel",
                "message": "你已经创建过同名频道了，不用重复创建。",
                "existing_id": dup.id,
                "name": name,
            })
    s = Series(**payload)
    s.user_id = owner_value(request)
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


@router.post("/design")
def design_channel(data: ChannelDesignRequest, request: Request, db: Session = Depends(get_db)):
    """自定义频道:用户一段描述 → AI 生成整套频道规则(仅预览,不落库)。

    命中红线(中国政治等)→ 返回 {ok: False, reason}(前端提示换方向)。前端预览/微调后再调
    POST /(create_series),把 rule 放进 channel_rule_json、industry_tag='custom_channel'。"""
    if multi_tenant_enabled() and not current_user_id(request):
        raise HTTPException(status_code=401, detail={"error": "unauthenticated"})
    from backend.lib.rate_limit import enforce_rate_limit
    enforce_rate_limit(request, "channel_design", per_min=10)  # 挡脚本狂刷 LLM
    from backend.services.channel_intelligence_service import design_channel_rule
    _set_topics_gateway_token(request)
    try:
        return design_channel_rule(data.description, data.output_language)
    except Exception:
        logger.warning("channel design failed", exc_info=True)
        raise HTTPException(status_code=502, detail={
            "error": "design_failed",
            "message": "AI 生成频道方向失败了，稍后再试一次~",
        })
    finally:
        _clear_topics_gateway_token()


@router.get("/{series_id}", response_model=SeriesRead)
def get_series(series_id: str, request: Request, db: Session = Depends(get_db)):
    s = _owned_series_or_404(series_id, db, request)
    return s


@router.patch("/{series_id}", response_model=SeriesRead)
def update_series(series_id: str, patch: SeriesUpdate, request: Request, db: Session = Depends(get_db)):
    s = _owned_series_or_404(series_id, db, request)
    for field, value in patch.model_dump(exclude_unset=True).items():
        setattr(s, field, value)
    db.commit()
    db.refresh(s)
    return s


@router.delete("/{series_id}", status_code=204)
def delete_series(series_id: str, request: Request, db: Session = Depends(get_db)):
    s = _owned_series_or_404(series_id, db, request)
    project_ids = [
        row[0] for row in db.query(Project.id).filter(Project.series_id == series_id).all()
    ]
    if project_ids:
        chunk_ids = [
            row[0] for row in db.query(Chunk.id).filter(Chunk.project_id.in_(project_ids)).all()
        ]
        if chunk_ids:
            db.query(Shot).filter(Shot.chunk_id.in_(chunk_ids)).delete(synchronize_session=False)
            db.query(Chunk).filter(Chunk.id.in_(chunk_ids)).delete(synchronize_session=False)
        db.query(Project).filter(Project.id.in_(project_ids)).delete(synchronize_session=False)
    db.query(BatchRun).filter(BatchRun.series_id == series_id).delete(synchronize_session=False)
    db.query(StudioSession).filter(StudioSession.series_id == series_id).delete(synchronize_session=False)
    db.delete(s)
    db.commit()


@router.get("/{series_id}/projects", response_model=list[ProjectRead])
def list_series_projects(series_id: str, request: Request, db: Session = Depends(get_db)):
    s = _owned_series_or_404(series_id, db, request)
    return (
        db.query(Project)
        .filter(Project.series_id == series_id)
        .order_by(Project.created_at.desc())
        .all()
    )


@router.get("/{series_id}/memory")
def get_series_memory(series_id: str, request: Request, db: Session = Depends(get_db)):
    _owned_series_or_404(series_id, db, request)
    return _intel().get_memory(db, series_id)


def _set_topics_gateway_token(request: Request) -> None:
    if not multi_tenant_enabled():
        return
    auth_hdr = request.headers.get("authorization") or ""
    token = auth_hdr[7:].strip() if auth_hdr[:7].lower() == "bearer " else None
    if token:
        from backend.lib.cloud_auth import get_default_auth
        get_default_auth().set_request_token(token)


def _clear_topics_gateway_token() -> None:
    if not multi_tenant_enabled():
        return
    from backend.lib.cloud_auth import get_default_auth
    get_default_auth().set_request_token(None)


@router.post("/{series_id}/dedupe")
def check_series_duplicate(
    series_id: str,
    body: ChannelCheckRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    _owned_series_or_404(series_id, db, request)
    from backend.lib.rate_limit import enforce_rate_limit
    enforce_rate_limit(request, "dedupe", per_min=20)  # H2:挡脚本狂刷 LLM 查重
    return _intel().check_duplicate(db, series_id, body.idea)


@router.post("/{series_id}/fit-check")
def check_series_fit(
    series_id: str,
    body: ChannelCheckRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    _owned_series_or_404(series_id, db, request)
    from backend.lib.rate_limit import enforce_rate_limit
    enforce_rate_limit(request, "fit_check", per_min=20)  # H2:挡脚本狂刷 LLM 契合检查
    return _intel().check_fit(db, series_id, body.idea, body.script_text)
