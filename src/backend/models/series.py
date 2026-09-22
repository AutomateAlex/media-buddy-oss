"""

A Series is a "frequency / theme folder" — e.g. "WW2 Stories" / "Finance Stories".
Projects belong to a Series. The Series owns the locked HardConfig (output format,
voice, BGM mood), SoftPrompt (director_prompt + learned_patterns) for memory
injection, and forbidden_topics for moderation.

- L1 LLM calls + L2 Memory: hard-isolated per Series via runtime prompt injection.
- L3 generated_cache + L4 asset_cache: soft-shared globally (cache key does NOT
  include series_id). Moderation re-runs per Series after cache hit, so kids
  series remains kids-safe even when reusing a lifestyle-series cache entry.
"""
from datetime import datetime, timezone
import uuid

from sqlalchemy import Boolean, Column, DateTime, Float, Integer, JSON, String

from backend.database import Base


class Series(Base):
    __tablename__ = "series"

    id          = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    # 多租户 owner — NULL on single-user desktop; set per logged-in user on cloud.
    user_id     = Column(String, index=True, nullable=True)
    name        = Column(String, nullable=False)
    description = Column(String)

    # HardConfig — new projects in this Series default-inherit these
    output_format          = Column(String, default="youtube_landscape")
    duration_target_seconds = Column(Integer, default=60)
    tts_provider           = Column(String, default="azure_yunyang")
    tts_voice              = Column(String)
    # 频道默认输出语言:"zh" | "en"。新项目继承(见 api/projects.py _SERIES_INHERITABLE_FIELDS)。
    output_language        = Column(String, default="zh")
    pipeline_mode          = Column(String, default="fast")  # fast | balanced | premium
    bgm_enabled            = Column(Boolean, default=True)
    bgm_mood_lock          = Column(String)  # e.g. "cinematic_dramatic"

    # SoftPrompt — injected into every LLM call for projects in this Series
    director_prompt = Column(String)

    # Phase 2.11k — visual-style preset binding (Pro-tier feature).
    # When set, the Style + Constraints blocks of every chunk's ai_prompt
    # are locked to the preset's values, so all videos in this Series
    # share a coherent visual identity (BBC nature / cinematic warm /
    # food macro / etc.). When NULL the LLM picks per chunk.
    style_preset_id = Column(String, nullable=True)

    # Industry & content safety
    industry_tag     = Column(String)              # "history" / "finance" / "kids" / ...
    nsfw_threshold   = Column(String, default="strict")  # strict | moderate | permissive
    forbidden_topics = Column(JSON, default=list)  # ["nudity", "minors", ...]

    # ChannelRule:positioning/subjects/angle_templates/anchors/off_domain/
    # concept_channel/closure_rule/label)。设了就让 channel_intelligence_service
    # 的 _rule_for 用这套动态定位(而非按 industry_tag 查硬编码预设),使自定义频道
    # 的选题/契合/收口全线达到 ≈ 预设频道的质量。NULL = 预设频道,走 CHANNEL_RULES。
    channel_rule_json = Column(JSON, nullable=True)

    # Accumulated learning — auto-populated by the pipeline as projects approve.
    # Schema: {
    #   "preferred_hooks": ["question", "data"],
    #   "preferred_pacing": "fast",
    #   "bad_sources": ["wikimedia"],   # sources that produced flagged clips
    #   "good_sources": ["pexels"],
    #   ...
    # }
    learned_patterns = Column(JSON, default=dict)

    # Quotas — enforced at batch-run start
    daily_video_cap    = Column(Integer, default=100)
    daily_cost_cap_usd = Column(Float, default=200.0)

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, onupdate=lambda: datetime.now(timezone.utc))
