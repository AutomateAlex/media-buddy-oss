"""

Two caches:
- AssetCacheEntry: stock search candidates by query+sources hash. TTL 7 days.
- GeneratedCacheEntry: AI-generated clips by prompt+provider+params hash. No TTL.
"""
from datetime import datetime, timezone

from sqlalchemy import Column, String, Integer, Float, DateTime, JSON

from backend.database import Base


class AssetCacheEntry(Base):
    __tablename__ = "asset_cache"

    cache_key = Column(String, primary_key=True)  # hash(query + sources_csv + filters)
    candidates_json = Column(JSON, default=list)  # list[FootageCandidate dict]
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    hit_count = Column(Integer, default=0)


class GeneratedCacheEntry(Base):
    __tablename__ = "generated_cache"

    cache_key = Column(String, primary_key=True)  # hash(prompt + provider + aspect + duration)
    provider = Column(String)
    prompt = Column(String)
    local_path = Column(String)
    score = Column(Float, default=0.0)
    cost_usd = Column(Float, default=0.0)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    reuse_count = Column(Integer, default=0)


class BlindObservationCacheEntry(Base):
    """

    Stock videos are immutable — once we've run Gemini Flash over a
    Pexels/stock/Library candidate's thumbnail, the result is
    reusable forever for that same (provider, source_id). Bump
    schema_version when BlindObservation pydantic schema changes
    incompatibly.
    """
    __tablename__ = "blind_observation_cache"

    cache_key = Column(String, primary_key=True)  # provider:source_id:vN
    provider = Column(String, nullable=False)
    source_id = Column(String, nullable=False)
    schema_version = Column(Integer, nullable=False)
    observation_json = Column(JSON, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    reuse_count = Column(Integer, default=0)
