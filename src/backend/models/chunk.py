"""Chunk + Shot ORM — Phase 2 chunk-state-machine."""
from datetime import datetime, timezone

from sqlalchemy import (
    Column, String, Integer, Float, DateTime, ForeignKey, JSON
)
from sqlalchemy.orm import relationship

from backend.database import Base


class Chunk(Base):
    """One sentence-level production unit. Holds 1..N Shots."""

    __tablename__ = "chunks"

    id = Column(String, primary_key=True)
    project_id = Column(String, ForeignKey("projects.id", ondelete="CASCADE"),
                        nullable=False, index=True)

    # Position in the script (0-based)
    chunk_index = Column(Integer, nullable=False)
    sentence = Column(String, nullable=False)

    # Adjacent sentences passed to LLM as context (avoids meaning loss)
    context_prev = Column(String, default="")
    context_next = Column(String, default="")

    # TTS slice (this sentence's narration audio)
    tts_audio_path = Column(String)
    tts_duration_seconds = Column(Float, default=0.0)

    # State machine: pending / generating_queries / text_critic_passed /
    #                approved / rejected_retry / failed
    status = Column(String, default="pending", index=True)
    retry_count = Column(Integer, default=0)
    last_error = Column(String)

    # Aggregate scores from this chunk's shots
    text_critic_avg = Column(Float)
    mm_critic_avg = Column(Float)

    # How many shots LLM decided this sentence needs (1..3)
    target_shot_count = Column(Integer, default=1)

    # Phase 2.6 — structured shot metadata used by ShotRouter
    shot_type = Column(String, default="atmosphere", index=True)
    # static | atmosphere | product | interior | establishing |
    # human_action | performance | interaction |
    # complex_motion | vfx
    is_hero_shot = Column(Integer, default=0)  # bool: 1 = hero, stricter scoring
    motion_tags = Column(JSON, default=list)   # ["hand_motion", "liquid_pouring", ...]
    visual_tags = Column(JSON, default=list)   # ["barista", "coffee", "cafe", ...]
    priority = Column(String, default="normal")  # low | normal | hero

    extra = Column(JSON, default=dict)

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, onupdate=lambda: datetime.now(timezone.utc))

    shots = relationship(
        "Shot", back_populates="chunk", cascade="all, delete-orphan",
        order_by="Shot.shot_index",
    )


class Shot(Base):
    """One visual cut. A Chunk has 1..N Shots (multi-layer sentences get >1)."""

    __tablename__ = "shots"

    id = Column(String, primary_key=True)
    chunk_id = Column(String, ForeignKey("chunks.id", ondelete="CASCADE"),
                      nullable=False, index=True)

    shot_index = Column(Integer, nullable=False)  # 0-based within the chunk

    # English director-style search query (from LLM)
    query = Column(String)
    # Critic score on the query alone (0-10)
    text_critic_score = Column(Float)
    text_critic_notes = Column(JSON, default=dict)
    text_critic_attempts = Column(Integer, default=0)

    # Selected footage clip after search
    selected_source = Column(String)        # "pexels" / "pixabay_video" / ...
    selected_source_id = Column(String)
    selected_source_url = Column(String)
    selected_local_path = Column(String)
    selected_clip_duration = Column(Float, default=0.0)
    selected_resolution = Column(String, default="")

    # Multimodal critic score against the actual rendered/found clip (0-10)
    mm_critic_score = Column(Float)
    mm_critic_notes = Column(JSON, default=dict)
    mm_critic_attempts = Column(Integer, default=0)

    cascade_depth = Column(Integer, default=3)
    prompts = Column(JSON, default=dict)     # {"L1": "...", "L5": "..."} per depth
    used_level = Column(String)              # "L1" / "L3" / "L5" — which level actually hit
    used_wave = Column(Integer)

    asset_type = Column(String, default="video")    # "video" | "image"
    motion_type = Column(String)                    # "ken_burns" / "push_in" / etc. (image only)

    director_intent = Column(String)    # Harness Contract intent excerpt
    selected_prompt = Column(String)    # the actual L_i prompt used for search
    verify_reason = Column(String)      # verifier's decision rationale
    confidence = Column(Float)          # "high"=1.0 / "low"=0.3 etc (heuristic mapping)
    fallback_reason = Column(String)    # "L5_image_empty_used_best_seen_video" etc

    status = Column(String, default="pending", index=True)
    last_error = Column(String)

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, onupdate=lambda: datetime.now(timezone.utc))

    chunk = relationship("Chunk", back_populates="shots")
