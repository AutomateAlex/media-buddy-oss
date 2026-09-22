from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.lib.user_context import is_admin_request, owner_value
from backend.schemas.script_intelligence import (
    ScriptListResponse,
    ScriptManuscriptCreate,
    ScriptManuscriptRead,
    ScriptRecommendationResponse,
)
from backend.services.script_intelligence_service import ScriptIntelligenceService

router = APIRouter()


def _svc() -> ScriptIntelligenceService:
    return ScriptIntelligenceService()


@router.post("/manuscripts", response_model=ScriptManuscriptRead, status_code=201)
def create_manuscript(data: ScriptManuscriptCreate, request: Request, db: Session = Depends(get_db)):
    payload = data.model_dump()
    payload["user_id"] = owner_value(request)
    # 全局共享格式池(public_seed)只有运营(admin)能灌,防用户互相污染。
    # 非 admin 想设 public_seed → 静默降级为私有稿。
    if payload.get("scope") == "public_seed" and not is_admin_request(request):
        payload["scope"] = "private_raw"
    return _svc().create(db, payload)


@router.get("/manuscripts", response_model=ScriptListResponse)
def list_manuscripts(
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    content_type: str | None = Query(None, pattern="^(short_video|youtube_long)$"),
    db: Session = Depends(get_db),
):
    rows, total = _svc().list(db, page=page, page_size=page_size, content_type=content_type, user_id=owner_value(request))
    return ScriptListResponse(
        items=[ScriptManuscriptRead.model_validate(row) for row in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/recommendations", response_model=ScriptRecommendationResponse)
def recommendations(
    request: Request,
    content_type: str = Query(..., pattern="^(short_video|youtube_long)$"),
    industry: str = "",
    platform: str = "",
    goal: str = "",
    limit: int = Query(5, ge=1, le=20),
    db: Session = Depends(get_db),
):
    return ScriptRecommendationResponse(
        items=_svc().recommendations(
            db,
            content_type=content_type,
            industry=industry,
            platform=platform,
            goal=goal,
            limit=limit,
            user_id=owner_value(request),
            # 🔒 只有运营(admin)能在推荐里看到全局公式库;普通用户拿不到 public_seed。
            include_public=is_admin_request(request),
        )
    )
