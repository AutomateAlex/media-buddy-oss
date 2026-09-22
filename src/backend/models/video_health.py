"""VideoHealth ORM — one post-render health/anomaly record per video.

Feeds the admin "suspect videos" board (colour-graded: red/orange/yellow), so
broken videos surface centrally instead of waiting for a customer complaint.
Created via SQLAlchemy create_all on mb-api startup (same as projects/chunks/shots).
"""
from datetime import datetime, timezone
import uuid

from sqlalchemy import Column, String, Integer, Float, Boolean, DateTime, JSON

from backend.database import Base


class VideoHealth(Base):
    __tablename__ = "video_health"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id = Column(String, nullable=False, index=True)
    video_job_id = Column(String, index=True)
    user_id = Column(String, index=True)

    output_format = Column(String)
    planned_seconds = Column(Integer)
    final_seconds = Column(Float)
    duration_ratio = Column(Float)
    script_issue = Column(String)
    local_fallback_ratio = Column(Float)
    adjacent_same_asset_count = Column(Integer)
    pass_rate_pct = Column(Float)
    query_degraded = Column(Boolean, default=False)

    verdict = Column(String, nullable=False, index=True)   # healthy | soft_flag | hard_fail
    flags = Column(JSON, default=list)                      # [{"type","level","detail"}]

    # ── 语速标定的反馈闭环(M6)──────────────────────────────────────
    # 写回去过**,几个月无人察觉。这几列就是那个闭环:每片记下预估与实际,
    # 按音色滚动统计中位偏离,超阈值告警(见 backend.lib.pacing_feedback)。
    # 实际时长复用上面已有的 final_seconds,不重复存。
    predicted_seconds = Column(Float)    # 出厂那一版的预估秒数(必须对应 chosen_version)
    voice = Column(String, index=True)   # 标定是按音色的,统计必须分音色
    speed = Column(Float)
    char_count = Column(Integer)         # tts_pacing.count_chars 口径(P9)
    chosen_version = Column(String)      # v1 / v2 / v3 —— 出厂的是哪一版
    degraded = Column(Boolean, default=False)   # 验收闸没能修好就出厂了

    # 多少钱、多久,以及**失败归因** —— 归因要能区分"收口没修好"和"长度压不下来",
    # 两者的后续动作完全不同(前者调 prompt/模型,后者调阈值/预算)。
    gate_calls = Column(Integer)
    gate_latency_ms = Column(Integer)
    gate_cost_usd = Column(Float)
    failure_reason = Column(String, index=True)
    rounds_used = Column(Integer)

    created_at = Column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )
