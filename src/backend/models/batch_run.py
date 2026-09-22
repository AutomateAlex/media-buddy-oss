"""Phase 2.7c — BatchRun ORM.

A BatchRun groups N Project rows that were launched together via
POST /api/series/{series_id}/batch. The Celery batch dispatcher updates
progress + cost in waves, and aborts new launches when daily_cost_cap_usd
is exceeded.
"""
from datetime import datetime, timezone
import uuid

from sqlalchemy import Column, DateTime, Float, ForeignKey, Integer, String

from backend.database import Base


class BatchRun(Base):
    __tablename__ = "batch_runs"

    id        = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    # 多租户 owner (denormalized from series for quick scoping). NULL on desktop.
    user_id   = Column(String, index=True, nullable=True)
    series_id = Column(String, ForeignKey("series.id"), nullable=False, index=True)

    # pending | running | completed | partial | aborted_cost_cap | failed | stopped
    status    = Column(String, default="pending")

    requested_count    = Column(Integer, default=0)
    completed_count    = Column(Integer, default=0)
    failed_count       = Column(Integer, default=0)
    moderation_blocked = Column(Integer, default=0)

    concurrency        = Column(Integer, default=4)
    daily_cost_cap_usd = Column(Float, default=200.0)
    total_cost_usd     = Column(Float, default=0.0)

    # 批次级出片设置(覆盖 series 默认,套用到本批每条 Project)。
    tts_provider     = Column(String)   # 选定音色 provider_key(如 azure_aria)
    duration_seconds = Column(Float)    # 目标时长(长视频可 20/30 分钟)
    output_language  = Column(String, default="zh")  # "zh" | "en"

    abort_reason = Column(String)

    celery_task_id = Column(String)
    started_at  = Column(DateTime)
    finished_at = Column(DateTime)
    created_at  = Column(DateTime, default=lambda: datetime.now(timezone.utc))
