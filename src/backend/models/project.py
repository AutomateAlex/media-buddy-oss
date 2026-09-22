from sqlalchemy import Column, String, DateTime, Boolean, Float, ForeignKey
from datetime import datetime, timezone
import uuid
from backend.database import Base

class Project(Base):
    __tablename__ = "projects"

    id            = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    # 多租户 owner — NULL on single-user desktop; set to the logged-in user on cloud SaaS.
    user_id       = Column(String, index=True, nullable=True)
    name          = Column(String, nullable=False)
    mode          = Column(String, nullable=False)      # "script" | "creative"
    status        = Column(String, default="pending")   # pending|running|completed|failed
    prompt        = Column(String)
    script_text   = Column(String)
    reference_url = Column(String)
    output_format = Column(String, default="youtube_landscape")
    # 短视频画面风格:"fill"(默认,竖屏裁剪铺满)| "blur"(背景虚化+正方形前景)。
    framing_style = Column(String, default="fill")
    duration_seconds = Column(Float)
    tts_provider  = Column(String, default="azure_yunyang")
    tts_speed     = Column(Float, default=1.0)
    include_subtitles = Column(Boolean, default=True)
    # 背景音乐开关。默认 False(不加 BGM)——躲 YouTube 版权/Content ID 投诉;
    # 客户想要才在出片设置里打开。为 None/缺省时管线按 False 处理(无音乐)。
    include_music = Column(Boolean, default=False)
    # 输出语言:"zh"(默认,中文出片)| "en"(出片前把定稿翻成英文 + 英文音色 TTS)。
    output_language = Column(String, default="zh")
    # 字幕语言(独立于 output_language):zh-Hans|zh-Hant|en|zh-Hans+en|zh-Hant+en。
    # NULL → 出片时跟随 output_language(旧行不受影响)。
    subtitle_language = Column(String, nullable=True)
    # 字幕字号缩放系数(客户在出片设置里拖拉杆自选)。1.0=标准;乘到默认字号上。
    # NULL/缺省 → 按 1.0(旧行不受影响)。范围前后端都夹在 [0.5, 2.0]。
    subtitle_font_scale = Column(Float, default=1.0)
    # 执行文案模式:客户甩定稿要求「一字不改」。为 True 时 pipeline 跳过 CTA 合规删句
    # (送礼/抽奖承诺照上,风险客户自担);默认 False,只有前端「执行文案」直出通道置 True。
    verbatim = Column(Boolean, default=False)
    current_stage = Column(String)
    pipeline_dir  = Column(String)
    output_path   = Column(String)
    celery_task_id = Column(String)

    # Phase 2.7a — Series membership. nullable until UI forces selection.
    series_id     = Column(String, ForeignKey("series.id"), nullable=True, index=True)

    # Phase 2.7c — Batch membership + cost tracking.
    batch_run_id  = Column(String, ForeignKey("batch_runs.id"), nullable=True, index=True)
    cost_usd      = Column(Float, default=0.0)

    # Phase 2.11d — Studio Agent conversation that produced this project
    # (when applicable — manual / form-mode projects leave it null). Lets the
    # ProjectDetail page replay the chat that birthed this video.
    studio_session_id = Column(String, ForeignKey("studio_sessions.id"), nullable=True, index=True)

    # Deprecated — kept for legacy DBs. New code uses series_id.
    campaign_id   = Column(String)

    created_at    = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at    = Column(DateTime, onupdate=lambda: datetime.now(timezone.utc))
    downloaded_at = Column(DateTime, nullable=True)   # 客户点下载时打戳;列表显示"已下载"

    # ── 存储保留(Storage Retention)———————————————————————————————
    # 桌面单机模式(MEDIA_BUDDY_MULTI_TENANT 关)不设过期、不清理 —— expires_at 留 None。
    expires_at        = Column(DateTime, nullable=True)   # 保留截止;None=不过期(桌面/未完成)
    storage_purged_at = Column(DateTime, nullable=True)
    # 保留意向:"auto_expire"(默认,到期自动清)|"keep"(用户★收藏,享聚合续存提醒;
    # 但收藏≠免费永久,没付费续存到期照删)。
    retention_intent  = Column(String, default="auto_expire")
