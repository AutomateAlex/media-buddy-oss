"""Cloud-ready script intelligence library.

This table is intentionally local-first for the desktop app, but the shape is
cloud-friendly: raw manuscripts can stay private, while distilled patterns can
be synced anonymously later.
"""
from datetime import datetime, timezone
import uuid

from sqlalchemy import Column, DateTime, Float, Integer, JSON, String, Text

from backend.database import Base


class ScriptManuscript(Base):
    __tablename__ = "script_manuscripts"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    # 多租户 owner — NULL on single-user desktop; set per logged-in user on cloud.
    user_id = Column(String, index=True, nullable=True)
    title = Column(String, nullable=False)
    content_type = Column(String, index=True, nullable=False)  # short_video | youtube_long
    language = Column(String, default="zh")

    # Privacy tier for future cloud sync:
    # private_raw: raw text only local/user-private
    # anonymized_pattern: distilled pattern can be pooled
    # public_seed: manually curated seed template
    scope = Column(String, default="private_raw", index=True)
    source = Column(String, default="manual")  # manual | import | generated | performance

    raw_text = Column(Text, nullable=False)
    template_summary = Column(Text)
    distilled_template = Column(JSON, default=dict)

    industry_tags = Column(JSON, default=list)
    platform_tags = Column(JSON, default=list)
    goal_tags = Column(JSON, default=list)
    style_tags = Column(JSON, default=list)
    structure_tags = Column(JSON, default=list)
    effect_tags = Column(JSON, default=list)

    quality_score = Column(Float, default=0.0)
    usage_count = Column(Integer, default=0)
    status = Column(String, default="indexed", index=True)  # pending | indexed | archived

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, onupdate=lambda: datetime.now(timezone.utc))
