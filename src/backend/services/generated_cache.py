"""

Stores hash → local_path map for AI-generated clips so the same prompt+model
combination on a future run reuses the file at zero cost. No TTL.
"""
from __future__ import annotations

import hashlib
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from backend.database import SessionLocal
from backend.models.cache import BlindObservationCacheEntry, GeneratedCacheEntry

logger = logging.getLogger(__name__)


# Bump when BlindObservation pydantic schema changes incompatibly. Cached
# rows with mismatched schema_version are ignored (read as miss).
BLIND_OBSERVATION_SCHEMA_VERSION = 1


def _blind_obs_key(provider: str, source_id: str, schema_version: int) -> str:
    """Stable cache key for blind observations. provider+source_id is
    candidate-unique; schema_version invalidates stale rows after schema
    changes."""
    return f"{provider}:{source_id}:v{schema_version}"


def get_blind_observation(
    provider: str,
    source_id: str,
    schema_version: int = BLIND_OBSERVATION_SCHEMA_VERSION,
) -> Optional[dict]:
    """Return cached observation_json dict if hit; None on miss. Caller
    re-validates against BlindObservation pydantic schema."""
    if not provider or not source_id:
        return None
    if os.environ.get(
        "MEDIA_BUDDY_DISABLE_OBS_CACHE", "0"
    ).strip().lower() in ("1", "true", "yes"):
        return None
    key = _blind_obs_key(provider, source_id, schema_version)
    db = SessionLocal()
    try:
        row = db.query(BlindObservationCacheEntry).filter_by(cache_key=key).first()
        if row is None:
            return None
        row.reuse_count = (row.reuse_count or 0) + 1
        db.commit()
        logger.info(
            "blind_obs_cache HIT %s:%s reuse_count=%d",
            provider, source_id, row.reuse_count,
        )
        return dict(row.observation_json) if row.observation_json else None
    finally:
        db.close()


def save_blind_observation(
    provider: str,
    source_id: str,
    observation_json: dict,
    schema_version: int = BLIND_OBSERVATION_SCHEMA_VERSION,
) -> None:
    """Persist observation. No-op if either ID is empty. Updates if key
    already present (e.g. schema_version bump replays)."""
    if not provider or not source_id or not observation_json:
        return
    key = _blind_obs_key(provider, source_id, schema_version)
    db = SessionLocal()
    try:
        row = db.query(BlindObservationCacheEntry).filter_by(cache_key=key).first()
        if row is None:
            row = BlindObservationCacheEntry(
                cache_key=key,
                provider=provider,
                source_id=source_id,
                schema_version=schema_version,
                observation_json=observation_json,
            )
            db.add(row)
        else:
            row.observation_json = observation_json
            row.created_at = datetime.now(timezone.utc)
        db.commit()
    finally:
        db.close()


def _hash_key(prompt: str, provider: str, aspect_ratio: str, duration: float) -> str:
    h = hashlib.sha256()
    h.update(prompt.strip().lower().encode("utf-8"))
    h.update(b"|")
    h.update(provider.encode("utf-8"))
    h.update(b"|")
    h.update(aspect_ratio.encode("utf-8"))
    h.update(b"|")
    h.update(f"{int(round(duration))}".encode("utf-8"))
    return h.hexdigest()[:24]


class GeneratedCache:
    """Persistent file cache for AI-generated clips."""

    def get(
        self, prompt: str, provider: str,
        aspect_ratio: str = "16:9", duration: float = 5.0,
    ) -> Optional[Path]:
        """Return Path if cache hit AND file still exists. Bumps reuse_count."""
        key = _hash_key(prompt, provider, aspect_ratio, duration)
        db = SessionLocal()
        try:
            row = db.query(GeneratedCacheEntry).filter_by(cache_key=key).first()
            if row is None:
                return None
            path = Path(row.local_path) if row.local_path else None
            if path is None or not path.exists():
                # File got cleaned up — drop the row
                db.delete(row)
                db.commit()
                return None
            row.reuse_count = (row.reuse_count or 0) + 1
            db.commit()
            logger.info(
                "generated_cache HIT %s: reuse_count=%d, saved $%.3f",
                provider, row.reuse_count, row.cost_usd or 0,
            )
            return path
        finally:
            db.close()

    def put(
        self, prompt: str, provider: str, local_path: Path,
        aspect_ratio: str = "16:9", duration: float = 5.0,
        score: float = 0.0, cost_usd: float = 0.0,
    ) -> None:
        if not Path(local_path).exists():
            return
        key = _hash_key(prompt, provider, aspect_ratio, duration)
        db = SessionLocal()
        try:
            row = db.query(GeneratedCacheEntry).filter_by(cache_key=key).first()
            if row is None:
                row = GeneratedCacheEntry(
                    cache_key=key,
                    provider=provider,
                    prompt=prompt,
                    local_path=str(local_path),
                    score=float(score),
                    cost_usd=float(cost_usd),
                )
                db.add(row)
            else:
                row.local_path = str(local_path)
                row.score = float(score) if score else row.score
                row.cost_usd = float(cost_usd) if cost_usd else row.cost_usd
                row.created_at = datetime.now(timezone.utc)
            db.commit()
        finally:
            db.close()
