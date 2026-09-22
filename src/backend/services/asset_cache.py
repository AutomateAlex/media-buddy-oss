"""

Saves stock search results (candidate metadata, NOT downloaded files) for 7 days
so a second project with similar prompts skips the external API hit.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from backend.database import SessionLocal
from backend.models.cache import AssetCacheEntry

logger = logging.getLogger(__name__)

CACHE_TTL = timedelta(days=7)


def _hash_key(query: str, sources: list[str], orientation: str) -> str:
    h = hashlib.sha256()
    h.update(query.strip().lower().encode("utf-8"))
    h.update(b"|")
    h.update(",".join(sorted(sources)).encode("utf-8"))
    h.update(b"|")
    h.update(orientation.encode("utf-8"))
    return h.hexdigest()[:24]


class AssetCache:
    """Wraps the AssetCacheEntry table with put/get + TTL eviction."""

    def get(
        self, query: str, sources: list[str], orientation: str = "landscape",
    ) -> Optional[list[dict[str, Any]]]:
        key = _hash_key(query, sources, orientation)
        db = SessionLocal()
        try:
            row = db.query(AssetCacheEntry).filter_by(cache_key=key).first()
            if row is None:
                return None
            # Naive datetime in DB → assume UTC
            created = row.created_at
            if created and created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            if created and datetime.now(timezone.utc) - created > CACHE_TTL:
                db.delete(row)
                db.commit()
                return None
            row.hit_count = (row.hit_count or 0) + 1
            db.commit()
            return list(row.candidates_json or [])
        finally:
            db.close()

    def put(
        self, query: str, sources: list[str],
        candidates: list[Any], orientation: str = "landscape",
    ) -> None:
        if not candidates:
            return
        key = _hash_key(query, sources, orientation)
        json_candidates = [_to_jsonable(c) for c in candidates]
        db = SessionLocal()
        try:
            row = db.query(AssetCacheEntry).filter_by(cache_key=key).first()
            if row is None:
                row = AssetCacheEntry(
                    cache_key=key,
                    candidates_json=json_candidates,
                )
                db.add(row)
            else:
                row.candidates_json = json_candidates
                row.created_at = datetime.now(timezone.utc)
            db.commit()
        finally:
            db.close()


def _to_jsonable(c: Any) -> dict[str, Any]:
    """Strip non-serializable fields (e.g., FootageCandidate._raw)."""
    if is_dataclass(c):
        d = asdict(c)
    elif isinstance(c, dict):
        d = dict(c)
    else:
        d = {"value": str(c)}
    d.pop("_raw", None)
    return d
