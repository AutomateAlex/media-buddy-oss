"""Phase 2.7a — Series CRUD schemas."""
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field


class SeriesCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    description: Optional[str] = None

    # HardConfig defaults
    output_format: str = "youtube_landscape"
    duration_target_seconds: int = 60
    tts_provider: str = "azure_yunyang"
    tts_voice: Optional[str] = None
    pipeline_mode: str = "fast"
    bgm_enabled: bool = True
    bgm_mood_lock: Optional[str] = None

    # SoftPrompt
    director_prompt: Optional[str] = None

    # Phase 2.11k — visual style preset (locks Style + Constraints
    # blocks of every chunk's ai_prompt for cross-video brand consistency)
    style_preset_id: Optional[str] = None

    # Industry / safety
    industry_tag: Optional[str] = None
    nsfw_threshold: str = "strict"
    forbidden_topics: list[str] = Field(default_factory=list)

    # 自定义频道 — AI 生成的动态频道规则(见 models/series.py)。预设频道留空。
    channel_rule_json: Optional[dict[str, Any]] = None

    # Quotas
    daily_video_cap: int = 100
    daily_cost_cap_usd: float = 200.0


class SeriesUpdate(BaseModel):
    """All fields optional — PATCH semantics."""
    name: Optional[str] = None
    description: Optional[str] = None
    output_format: Optional[str] = None
    duration_target_seconds: Optional[int] = None
    tts_provider: Optional[str] = None
    tts_voice: Optional[str] = None
    pipeline_mode: Optional[str] = None
    bgm_enabled: Optional[bool] = None
    bgm_mood_lock: Optional[str] = None
    director_prompt: Optional[str] = None
    style_preset_id: Optional[str] = None
    industry_tag: Optional[str] = None
    nsfw_threshold: Optional[str] = None
    forbidden_topics: Optional[list[str]] = None
    channel_rule_json: Optional[dict[str, Any]] = None
    learned_patterns: Optional[dict[str, Any]] = None
    daily_video_cap: Optional[int] = None
    daily_cost_cap_usd: Optional[float] = None


class SeriesRead(BaseModel):
    id: str
    name: str
    description: Optional[str] = None
    output_format: str
    duration_target_seconds: int
    tts_provider: str
    tts_voice: Optional[str] = None
    pipeline_mode: str
    bgm_enabled: bool
    bgm_mood_lock: Optional[str] = None
    director_prompt: Optional[str] = None
    style_preset_id: Optional[str] = None
    industry_tag: Optional[str] = None
    nsfw_threshold: str
    forbidden_topics: list[str] = Field(default_factory=list)
    channel_rule_json: Optional[dict[str, Any]] = None
    learned_patterns: dict[str, Any] = Field(default_factory=dict)
    daily_video_cap: int
    daily_cost_cap_usd: float
    created_at: datetime
    updated_at: Optional[datetime] = None

    model_config = {"from_attributes": True}
