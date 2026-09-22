"""Phase 2.9a — Local visual asset library.

A LibraryClip is a video file we've ingested from somewhere (Pexels download,
AI generation, or a backfilled clip from an old pipeline run) and want to
keep around for reuse. Clips live under ~/.media-buddy-oss/library/clips/ and
are indexed by tags + category + LLM-generated description.

Lookup priority in the pipeline:
  L0  library_clips (this table)   ← fastest, free
  L1  generated_cache (AI outputs)
  L2  asset_cache (stock metadata, TTL 7d)
  L3  remote stock APIs (Pexels/Pixabay/Coverr/...)
  L4  AI generation
"""
from datetime import datetime, timezone
import uuid

from sqlalchemy import Column, DateTime, Float, Integer, JSON, String

from backend.database import Base


class LibraryClip(Base):
    __tablename__ = "library_clips"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    # 多租户 owner — NULL on desktop / globally-shared clips; per-user on cloud.
    user_id = Column(String, index=True, nullable=True)

    # File location — canonical path under ~/.media-buddy-oss/library/clips/
    local_path     = Column(String, nullable=False, unique=True)
    thumbnail_path = Column(String)  # ~/.media-buddy-oss/library/thumbnails/<id>.jpg
    file_hash      = Column(String, index=True)

    # Provenance
    source     = Column(String, index=True)   # "pexels" | "pixabay_video" | "fal_ai_seedance" | ...
    source_id  = Column(String, index=True)
    source_url = Column(String)

    # Tech specs (filled at ingest time via ffprobe; cheap)
    duration_seconds = Column(Float, default=0.0)
    width            = Column(Integer, default=0)
    height           = Column(Integer, default=0)
    aspect_ratio     = Column(String, index=True)   # "16:9" | "9:16" | "1:1" | "4:3"

    # LLM tagging (filled async by BackgroundWorker in idle cycles)
    category     = Column(String, index=True)   # "people"|"nature"|"urban"|"interior"|"food"|"business"|"abstract"|"tech"|"sports"|"vehicles"|"animals"|"textures"
    tags         = Column(JSON, default=list)   # ["barista","coffee","cafe","steam","morning"]
    description  = Column(String)               # LLM prose
    mood_tags    = Column(JSON, default=list)   # ["calm","warm","intimate"]
    motion_tags  = Column(JSON, default=list)   # ["slow_pan","liquid_pour","static"]

    # Tagging lifecycle
    tag_status   = Column(String, default="pending", index=True)   # pending|tagging|indexed|failed
    tag_provider = Column(String)                                  # "qwen3-vl" | ...
    tag_cost_usd = Column(Float, default=0.0)
    tag_error    = Column(String)

    # Bookkeeping
    discovered_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    indexed_at    = Column(DateTime)
    used_count    = Column(Integer, default=0, index=True)
    last_used_at  = Column(DateTime)
