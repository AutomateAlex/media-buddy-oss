from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field


class ScriptManuscriptCreate(BaseModel):
    title: str
    raw_text: str
    content_type: str = Field(pattern="^(short_video|youtube_long)$")
    language: str = "zh"
    scope: str = "private_raw"
    source: str = "manual"
    industry_tags: list[str] = []
    platform_tags: list[str] = []
    goal_tags: list[str] = []
    style_tags: list[str] = []
    structure_tags: list[str] = []
    effect_tags: list[str] = []
    quality_score: float = 0.0


class ScriptManuscriptRead(BaseModel):
    id: str
    title: str
    content_type: str
    language: str
    scope: str
    source: str
    raw_text: str
    template_summary: Optional[str] = None
    distilled_template: dict[str, Any] = {}
    industry_tags: list[str] = []
    platform_tags: list[str] = []
    goal_tags: list[str] = []
    style_tags: list[str] = []
    structure_tags: list[str] = []
    effect_tags: list[str] = []
    quality_score: float = 0.0
    usage_count: int = 0
    status: str
    created_at: datetime
    updated_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class ScriptRecommendation(BaseModel):
    id: str
    title: str
    content_type: str
    score: float
    template_summary: str
    distilled_template: dict[str, Any]
    tags: dict[str, list[str]]


class ScriptRecommendationResponse(BaseModel):
    items: list[ScriptRecommendation]


class ScriptListResponse(BaseModel):
    items: list[ScriptManuscriptRead]
    total: int
    page: int
    page_size: int
